import re
import warnings

from collections import OrderedDict
from functools import reduce

from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramWordSimilarity,
)
from django.db import (
    NotSupportedError,
    ProgrammingError,
    connections,
    router,
    transaction,
)
from django.db.models import (
    Avg,
    Case,
    Count,
    F,
    FloatField,
    Func,
    IntegerField,
    Manager,
    TextField,
    Value,
    When,
)
from django.db.models.constants import LOOKUP_SEP
from django.db.models.functions import Cast, Greatest, Length
from django.db.models.sql.subqueries import InsertQuery
from django.utils.encoding import force_str
from django.utils.functional import cached_property

from modelsearch.conf import get_app_config

from ....index import AutocompleteField, RelatedFields, SearchField, get_indexed_models
from ....query import And, Boost, Fuzzy, MatchAll, Not, Or, Phrase, PlainText
from ....utils import (
    ADD,
    MUL,
    get_content_type_pk,
    get_descendants_content_types_pks,
)
from ...base import (
    BaseIndex,
    BaseSearchBackend,
    BaseSearchQueryCompiler,
    BaseSearchResults,
    FilterFieldError,
)
from .query import Lexeme
from .weights import get_sql_weights, get_weight


IndexEntry = get_app_config().get_model("IndexEntry", require_ready=False)
EMPTY_VECTOR = SearchVector(Value("", output_field=TextField()))

DEFAULT_FUZZY_SIMILARITY_THRESHOLD = 0.3
DEFAULT_FUZZY_PREFIX_BOOST = 0.0  # Multiplier bonus when field starts with query
DEFAULT_FUZZY_ALGORITHM = "trigram"  # "trigram" or "levenshtein"


class FUnaccent(Func):
    """Calls the immutable f_unaccent() SQL wrapper for use in trigram queries and indexes."""

    function = "f_unaccent"
    output_field = TextField()


class LevenshteinDistance(Func):
    """PostgreSQL levenshtein() from fuzzystrmatch extension."""

    function = "levenshtein"
    output_field = IntegerField()


class WordLevenshteinSimilarity(Func):
    """
    Word-level Levenshtein similarity for PostgreSQL.

    Splits a text field into words and computes the normalized Levenshtein
    similarity against the best matching word. This mirrors how
    TrigramWordSimilarity compares against the best word/substring.

    Returns a float between 0 and 1 (1 = exact word match).
    """

    # Split on whitespace and hyphens so "lave-vaisselle" is matched word by word
    _split_pattern = "E'[\\\\s\\\\-]+'"
    template = (
        "(1.0 - ("  # noqa: S608
        "SELECT MIN(levenshtein(SUBSTR(LOWER(word)::varchar(255), 1, 255), %(query)s))"
        "::double precision"
        " FROM unnest(regexp_split_to_array(%(field)s::text, "
        + _split_pattern
        + ")) AS word"
        " WHERE LENGTH(word) > 0"
        ") / GREATEST("
        "(SELECT LENGTH(word)"
        " FROM unnest(regexp_split_to_array(%(field)s::text, "
        + _split_pattern
        + ")) AS word"
        " WHERE LENGTH(word) > 0"
        " ORDER BY levenshtein(SUBSTR(LOWER(word)::varchar(255), 1, 255), %(query)s) ASC"
        " LIMIT 1"
        ")::double precision,"
        " %(query_len)s, 1.0))"
    )
    output_field = FloatField()

    def __init__(self, expression, query_string, **extra):
        self.query_string = query_string.lower()
        self.query_len = float(len(query_string))
        super().__init__(expression, **extra)

    def as_sql(self, compiler, connection, **extra_context):
        expressions = []
        expression_params = []
        for arg in self.source_expressions:
            arg_sql, arg_params = compiler.compile(arg)
            expressions.append(arg_sql)
            expression_params.extend(arg_params)

        template = self.template % {
            "field": expressions[0],
            "query": "%s",
            "query_len": "%s",
        }
        # query appears 2 times in the template, query_len once
        params = expression_params + [
            self.query_string,
            self.query_string,
            self.query_len,
        ]
        return template, params


class ObjectIndexer:
    """
    Responsible for extracting data from an object to be inserted into the index.
    """

    def __init__(self, obj, backend):
        self.obj = obj
        self.search_fields = obj.get_search_fields()
        self.config = backend.config
        self.autocomplete_config = backend.autocomplete_config

    def prepare_value(self, value):
        if isinstance(value, str):
            return value

        elif isinstance(value, list):
            return ", ".join(self.prepare_value(item) for item in value)

        elif isinstance(value, dict):
            return ", ".join(self.prepare_value(item) for item in value.values())

        return force_str(value)

    def prepare_field(self, obj, field):
        if isinstance(field, SearchField):
            yield (
                field,
                get_weight(field.boost),
                self.prepare_value(field.get_value(obj)),
            )

        elif isinstance(field, AutocompleteField):
            # AutocompleteField does not define a boost parameter, so use a base weight of 'D'
            yield (field, "D", self.prepare_value(field.get_value(obj)))

        elif isinstance(field, RelatedFields):
            sub_obj = field.get_value(obj)
            if sub_obj is None:
                return

            if isinstance(sub_obj, Manager):
                sub_objs = sub_obj.all()

            else:
                if callable(sub_obj):
                    sub_obj = sub_obj()

                sub_objs = [sub_obj]

            for sub_obj in sub_objs:
                for sub_field in field.fields:
                    yield from self.prepare_field(sub_obj, sub_field)

    def as_vector(self, texts, for_autocomplete=False):
        """
        Converts an array of strings into a SearchVector that can be indexed.
        """
        texts = [(text.strip(), weight) for text, weight in texts]
        texts = [(text, weight) for text, weight in texts if text]

        if not texts:
            return EMPTY_VECTOR

        search_config = self.autocomplete_config if for_autocomplete else self.config

        return ADD(
            [
                SearchVector(
                    Value(text, output_field=TextField()),
                    weight=weight,
                    config=search_config,
                )
                for text, weight in texts
            ]
        )

    @cached_property
    def id(self):
        """
        Returns the value to use as the ID of the record in the index
        """
        return force_str(self.obj.pk)

    @cached_property
    def title(self):
        """
        Returns all values to index as "title". This is the value of all SearchFields that have the field_name 'title'
        """
        texts = []
        for field in self.search_fields:
            for current_field, boost, value in self.prepare_field(self.obj, field):
                if (
                    isinstance(current_field, SearchField)
                    and current_field.field_name == "title"
                ):
                    texts.append((value, boost))

        return self.as_vector(texts)

    @cached_property
    def body(self):
        """
        Returns all values to index as "body". This is the value of all SearchFields excluding the title
        """
        texts = []
        for field in self.search_fields:
            for current_field, boost, value in self.prepare_field(self.obj, field):
                if (
                    isinstance(current_field, SearchField)
                    and not current_field.field_name == "title"
                ):
                    texts.append((value, boost))

        return self.as_vector(texts)

    @cached_property
    def autocomplete(self):
        """
        Returns all values to index as "autocomplete". This is the value of all AutocompleteFields
        """
        texts = []
        for field in self.search_fields:
            for current_field, boost, value in self.prepare_field(self.obj, field):
                if isinstance(current_field, AutocompleteField):
                    texts.append((value, boost))

        return self.as_vector(texts, for_autocomplete=True)

    @cached_property
    def title_text(self):
        """
        Plain text for title fields, for trigram/fuzzy search.
        """
        texts = []
        for field in self.search_fields:
            for current_field, _boost, value in self.prepare_field(self.obj, field):
                if (
                    isinstance(current_field, SearchField)
                    and current_field.field_name == "title"
                ):
                    text = value.strip()
                    if text:
                        texts.append(text)
        return " ".join(texts)

    @cached_property
    def body_text(self):
        """
        Plain text for body fields, for trigram/fuzzy search.
        """
        texts = []
        for field in self.search_fields:
            for current_field, _boost, value in self.prepare_field(self.obj, field):
                if (
                    isinstance(current_field, SearchField)
                    and current_field.field_name != "title"
                ):
                    text = value.strip()
                    if text:
                        texts.append(text)
        return " ".join(texts)


class PostgresIndex(BaseIndex):
    def __init__(self, backend):
        super().__init__(backend)

        self.read_connection = connections[router.db_for_read(IndexEntry)]
        self.write_connection = connections[router.db_for_write(IndexEntry)]

        if (
            self.read_connection.vendor != "postgresql"
            or self.write_connection.vendor != "postgresql"
        ):
            raise NotSupportedError(
                "You must select a PostgreSQL database to use PostgreSQL search."
            )

        self.entries = IndexEntry._default_manager.all()

    def _refresh_title_norms(self, full=False):
        """
        Refreshes the value of the title_norm field.

        This needs to be set to 'lavg/ld' where:
         - lavg is the average length of titles in all documents (also in terms)
         - ld is the length of the title field in this document (in terms)
        """

        lavg = (
            self.entries.annotate(title_length=Length("title"))
            .filter(title_length__gt=0)
            .aggregate(Avg("title_length"))["title_length__avg"]
        )

        if full:
            # Update the whole table
            # This is the most accurate option but requires a full table rewrite
            # so we can't do it too often as it could lead to locking issues.
            entries = self.entries

        else:
            # Only update entries where title_norm is 1.0
            # This is the default value set on new entries.
            # It's possible that other entries could have this exact value but there shouldn't be too many of those
            entries = self.entries.filter(title_norm=1.0)

        entries.annotate(title_length=Length("title")).filter(
            title_length__gt=0
        ).update(title_norm=lavg / F("title_length"))

    def delete_stale_model_entries(self, model):
        existing_pks = model._default_manager.annotate(
            object_id=Cast("pk", TextField())
        ).values("object_id")
        content_types_pks = get_descendants_content_types_pks(model)
        stale_entries = self.entries.filter(
            content_type_id__in=content_types_pks
        ).exclude(object_id__in=existing_pks)
        stale_entries.delete()

    def delete_stale_entries(self):
        for model in get_indexed_models():
            # We don’t need to delete stale entries for non-root models,
            # since we already delete them by deleting roots.
            if not model._meta.parents:
                self.delete_stale_model_entries(model)

    def add_items(self, model, objs):
        search_fields = model.get_search_fields()
        if not search_fields:
            return

        indexers = [ObjectIndexer(obj, self.backend) for obj in objs]

        # TODO: Delete unindexed objects while dealing with proxy models.
        if not indexers:
            return

        content_type_pk = get_content_type_pk(model)
        compiler = InsertQuery(IndexEntry).get_compiler(
            connection=self.write_connection
        )
        title_sql = []
        autocomplete_sql = []
        body_sql = []
        data_params = []

        for indexer in indexers:
            data_params.extend((content_type_pk, indexer.id))

            # Compile title value
            value = compiler.prepare_value(
                IndexEntry._meta.get_field("title"), indexer.title
            )
            sql, params = value.as_sql(compiler, self.write_connection)
            title_sql.append(sql)
            data_params.extend(params)

            # Compile autocomplete value
            value = compiler.prepare_value(
                IndexEntry._meta.get_field("autocomplete"), indexer.autocomplete
            )
            sql, params = value.as_sql(compiler, self.write_connection)
            autocomplete_sql.append(sql)
            data_params.extend(params)

            # Compile body value
            value = compiler.prepare_value(
                IndexEntry._meta.get_field("body"), indexer.body
            )
            sql, params = value.as_sql(compiler, self.write_connection)
            body_sql.append(sql)
            data_params.extend(params)

            # Plain text values for fuzzy search
            data_params.append(indexer.title_text)
            data_params.append(indexer.body_text)

        data_sql = ", ".join(
            [
                f"(%s, %s, {a}, {b}, {c}, 1.0, %s, %s)"
                for a, b, c in zip(title_sql, autocomplete_sql, body_sql, strict=True)
            ]
        )

        with self.write_connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {IndexEntry._meta.db_table} (content_type_id, object_id, title, autocomplete, body, title_norm, title_text, body_text)
                (VALUES {data_sql})
                ON CONFLICT (content_type_id, object_id)
                DO UPDATE SET title = EXCLUDED.title,
                              title_norm = 1.0,
                              autocomplete = EXCLUDED.autocomplete,
                              body = EXCLUDED.body,
                              title_text = EXCLUDED.title_text,
                              body_text = EXCLUDED.body_text
                """,
                data_params,
            )

        self._refresh_title_norms()

    def delete_item(self, item):
        item.index_entries.all()._raw_delete(using=self.write_connection.alias)

    def reset(self):
        for connection in [
            connection
            for connection in connections.all()
            if connection.vendor == "postgresql"
        ]:
            IndexEntry._default_manager.all()._raw_delete(using=connection.alias)


class PostgresSearchQueryCompiler(BaseSearchQueryCompiler):
    DEFAULT_OPERATOR = "and"
    LAST_TERM_IS_PREFIX = False
    TARGET_SEARCH_FIELD_TYPE = SearchField
    HANDLES_ORDER_BY_EXPRESSIONS = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        local_search_fields = self.get_search_fields_for_model()

        # Due to a Django bug, arrays are not automatically converted
        # when we use WEIGHTS_VALUES.
        self.sql_weights = get_sql_weights()

        if self.fields is None:
            # search over the fields defined on the current model
            self.search_fields = local_search_fields
        else:
            # build a search_fields set from the passed definition,
            # which may involve traversing relations
            self.search_fields = {
                field_lookup: self.get_search_field(
                    field_lookup, fields=local_search_fields
                )
                for field_lookup in self.fields
            }

    def get_config(self, backend):
        return backend.config

    def get_search_fields_for_model(self):
        return self.queryset.model.get_searchable_search_fields()

    def get_search_field(self, field_lookup, fields=None):
        if fields is None:
            fields = self.search_fields

        if LOOKUP_SEP in field_lookup:
            field_lookup, sub_field_name = field_lookup.split(LOOKUP_SEP, 1)
        else:
            sub_field_name = None

        for field in fields:
            if (
                isinstance(field, self.TARGET_SEARCH_FIELD_TYPE)
                and field.field_name == field_lookup
            ):
                return field

            # Note: Searching on a specific related field using
            # `.search(fields=…)` is not yet supported by Wagtail.
            # This method anticipates by already implementing it.
            # FIXME: this doesn't work because the list we're looping over comes from
            # get_search_fields_for_model, which only returns `SearchField` records, not `RelatedFields`
            if (
                isinstance(field, RelatedFields)
                and field.field_name == field_lookup
                and sub_field_name is not None
            ):
                return self.get_search_field(
                    sub_field_name, field.fields
                )  # pragma: no cover

    def build_tsquery_content(self, query, config=None, invert=False):
        if isinstance(query, PlainText):
            terms = re.split(r"[\s\-]+", query.query_string)
            if not terms:
                return None

            last_term = terms.pop()

            lexemes = Lexeme(last_term, invert=invert, prefix=self.LAST_TERM_IS_PREFIX)
            for term in terms:
                new_lexeme = Lexeme(term, invert=invert)

                if query.operator == "and":
                    lexemes &= new_lexeme
                else:
                    lexemes |= new_lexeme

            return SearchQuery(lexemes, search_type="raw", config=config)

        elif isinstance(query, Phrase):
            return SearchQuery(query.query_string, search_type="phrase", config=config)

        elif isinstance(query, Boost):
            # Not supported
            msg = "The Boost query is not supported by the PostgreSQL search backend."
            warnings.warn(msg, RuntimeWarning, stacklevel=2)

            return self.build_tsquery_content(
                query.subquery, config=config, invert=invert
            )

        elif isinstance(query, Not):
            return self.build_tsquery_content(
                query.subquery, config=config, invert=not invert
            )

        elif isinstance(query, (And, Or)):
            # If this part of the query is inverted, we swap the operator and
            # pass down the inversion state to the child queries.
            # This works thanks to De Morgan's law.
            #
            # For example, the following query:
            #
            #   Not(And(Term("A"), Term("B")))
            #
            # Is equivalent to:
            #
            #   Or(Not(Term("A")), Not(Term("B")))
            #
            # It's simpler to code it this way as we only need to store the
            # invert status of the terms rather than all the operators.

            subquery_lexemes = [
                self.build_tsquery_content(subquery, config=config, invert=invert)
                for subquery in query.subqueries
            ]

            is_and = isinstance(query, And)

            if invert:
                is_and = not is_and

            if is_and:
                return reduce(lambda a, b: a & b, subquery_lexemes)
            else:
                return reduce(lambda a, b: a | b, subquery_lexemes)

        raise NotImplementedError(
            f"`{query.__class__.__name__}` is not supported by the PostgreSQL search backend."
        )

    def build_tsquery(self, query, config=None):
        return self.build_tsquery_content(query, config=config)

    def build_tsrank(self, vector, query, config=None, boost=1.0):
        if isinstance(query, (Phrase, PlainText, Not)):
            rank_expression = SearchRank(
                vector,
                self.build_tsquery(query, config=config),
                weights=self.sql_weights,
            )

            if boost != 1.0:
                rank_expression *= boost

            return rank_expression

        elif isinstance(query, Boost):
            boost *= query.boost
            return self.build_tsrank(vector, query.subquery, config=config, boost=boost)

        elif isinstance(query, And):
            return (
                MUL(
                    1 + self.build_tsrank(vector, subquery, config=config, boost=boost)
                    for subquery in query.subqueries
                )
                - 1
            )

        elif isinstance(query, Or):
            return ADD(
                self.build_tsrank(vector, subquery, config=config, boost=boost)
                for subquery in query.subqueries
            ) / (len(query.subqueries) or 1)

        raise NotImplementedError(
            f"`{query.__class__.__name__}` is not supported by the PostgreSQL search backend."
        )

    def get_index_vectors(self, search_query):
        return [
            (F("index_entries__title"), F("index_entries__title_norm")),
            (F("index_entries__body"), 1.0),
        ]

    def get_fields_vectors(self, search_query):
        return [
            (
                SearchVector(
                    field_lookup,
                    config=search_query.config,
                ),
                search_field.boost,
            )
            for field_lookup, search_field in self.search_fields.items()
        ]

    def get_search_vectors(self, search_query):
        if self.fields is None:
            return self.get_index_vectors(search_query)

        else:
            return self.get_fields_vectors(search_query)

    def _build_rank_expression(self, vectors, config):
        rank_expressions = [
            self.build_tsrank(vector, self.query, config=config) * boost
            for vector, boost in vectors
        ]

        rank_expression = rank_expressions[0]
        for other_rank_expression in rank_expressions[1:]:
            rank_expression += other_rank_expression

        return rank_expression

    def _apply_ordering_and_scoring(
        self, queryset, rank_expression, start, stop, score_field=None
    ):
        """
        Apply ordering, scoring annotation, and slicing to a queryset.

        This is shared logic between regular search and fuzzy search.
        """
        if self.order_by_relevance:
            queryset = queryset.order_by(rank_expression.desc(), "-pk")
        elif not queryset.query.order_by:
            # Adds a default ordering to avoid issue #3729.
            queryset = queryset.order_by("-pk")
            rank_expression = F("pk")

        if score_field is not None:
            queryset = queryset.annotate(**{score_field: rank_expression})

        return queryset[start:stop]

    def _build_fuzzy_queryset(self, config, backend):
        """
        Build a queryset for fuzzy search using IndexEntry text fields.

        Queries index_entries__title_text and index_entries__body_text
        instead of model fields directly, consistent with how PlainText
        and Phrase searches use the IndexEntry table.

        Requires the pg_trgm extension (for trigram algorithm) or the
        fuzzystrmatch extension (for levenshtein algorithm).

        When query.unaccent is True, wraps field expressions and the search
        string in f_unaccent() for accent-insensitive matching. Requires the
        unaccent extension and f_unaccent() function to be installed via
        the enable_trigram or enable_unaccent management command.
        """
        search_string = self.query.query_string
        use_unaccent = getattr(self.query, "unaccent", False)
        prefix_boost = backend.fuzzy_prefix_boost
        fuzzy_algorithm = backend.fuzzy_algorithm
        threshold = backend.fuzzy_similarity_threshold

        title_field = "index_entries__title_text"
        body_field = "index_entries__body_text"

        if use_unaccent:
            norm_search_string = FUnaccent(
                Value(search_string, output_field=TextField())
            )
            if fuzzy_algorithm == "levenshtein":
                # Annotate with unaccented versions then pass annotation names to similarity
                queryset = self.queryset.annotate(
                    _title_unaccented=FUnaccent(F(title_field)),
                    _body_unaccented=FUnaccent(F(body_field)),
                )
                title_similarity = WordLevenshteinSimilarity(
                    F("_title_unaccented"), search_string
                )
                body_similarity = WordLevenshteinSimilarity(
                    F("_body_unaccented"), search_string
                )
            else:
                # Annotate with unaccented versions then pass annotation names to TrigramWordSimilarity
                queryset = self.queryset.annotate(
                    _title_unaccented=FUnaccent(F(title_field)),
                    _body_unaccented=FUnaccent(F(body_field)),
                )
                title_similarity = TrigramWordSimilarity(
                    norm_search_string, "_title_unaccented"
                )
                body_similarity = TrigramWordSimilarity(
                    norm_search_string, "_body_unaccented"
                )
        else:
            queryset = self.queryset
            if fuzzy_algorithm == "levenshtein":
                title_similarity = WordLevenshteinSimilarity(
                    F(title_field), search_string
                )
                body_similarity = WordLevenshteinSimilarity(
                    F(body_field), search_string
                )
            else:
                title_similarity = TrigramWordSimilarity(search_string, title_field)
                body_similarity = TrigramWordSimilarity(search_string, body_field)

        # Raw (unboosted) similarity for threshold filtering.
        raw_similarity = Greatest(
            title_similarity,
            body_similarity,
            output_field=FloatField(),
        )

        # Ranked similarity for ordering: title gets a boost from title_norm.
        ranked_similarity = Greatest(
            title_similarity * F("index_entries__title_norm"),
            body_similarity,
            output_field=FloatField(),
        )

        if prefix_boost > 0:
            prefix_multiplier = Case(
                When(
                    **{f"{title_field}__istartswith": search_string},
                    then=Value(1.0 + prefix_boost),
                ),
                default=Value(1.0),
                output_field=FloatField(),
            )
            ranked_similarity = ranked_similarity * prefix_multiplier

        queryset = queryset.annotate(
            _fuzzy_raw_similarity=raw_similarity,
            _fuzzy_similarity=ranked_similarity,
        ).filter(_fuzzy_raw_similarity__gte=threshold)

        return queryset, F("_fuzzy_similarity")

    def search(self, config, start, stop, score_field=None, backend=None):
        # TODO: Handle MatchAll nested inside other search query classes.
        if isinstance(self.query, MatchAll):
            return self.queryset[start:stop]

        elif isinstance(self.query, Not) and isinstance(self.query.subquery, MatchAll):
            return self.queryset.none()

        elif isinstance(self.query, Fuzzy):
            queryset, rank_expression = self._build_fuzzy_queryset(config, backend)
            return self._apply_ordering_and_scoring(
                queryset, rank_expression, start, stop, score_field
            )

        search_query = self.build_tsquery(self.query, config=config)
        vectors = self.get_search_vectors(search_query)
        rank_expression = self._build_rank_expression(vectors, config)

        combined_vector = vectors[0][0]
        for vector, _boost in vectors[1:]:
            combined_vector = combined_vector._combine(vector, "||", False)

        queryset = self.queryset.annotate(_vector_=combined_vector).filter(
            _vector_=search_query
        )

        return self._apply_ordering_and_scoring(
            queryset, rank_expression, start, stop, score_field
        )


class PostgresAutocompleteQueryCompiler(PostgresSearchQueryCompiler):
    LAST_TERM_IS_PREFIX = True
    TARGET_SEARCH_FIELD_TYPE = AutocompleteField

    def get_config(self, backend):
        return backend.autocomplete_config

    def get_search_fields_for_model(self):
        return self.queryset.model.get_autocomplete_search_fields()

    def get_index_vectors(self, search_query):
        return [(F("index_entries__autocomplete"), 1.0)]

    def get_fields_vectors(self, search_query):
        return [
            (
                SearchVector(
                    field_lookup,
                    config=search_query.config,
                    weight="D",
                ),
                1.0,
            )
            for field_lookup, search_field in self.search_fields.items()
        ]


class PostgresSearchResults(BaseSearchResults):
    def get_queryset(self):
        return self.query_compiler.search(
            self.query_compiler.get_config(self.backend),
            self.start,
            self.stop,
            score_field=self._score_field,
            backend=self.backend,
        )

    def _do_search(self):
        try:
            return list(self.get_queryset())
        except ProgrammingError as e:
            self._handle_missing_postgres_extension(e)

    def _do_count(self):
        try:
            return self.get_queryset().count()
        except ProgrammingError as e:
            self._handle_missing_postgres_extension(e)

    def _handle_missing_postgres_extension(self, error):
        """
        Handle missing extension in PostgreSQL and provide helpful messages.
        """
        error_message = str(error).lower()
        if "does not exist" in error_message:
            if "similarity" in error_message or "word_similarity" in error_message:
                raise NotSupportedError(
                    "Fuzzy search requires the PostgreSQL pg_trgm extension. "
                    "Enable it by running: python manage.py enable_trigram"
                ) from error
            if "levenshtein" in error_message:
                raise NotSupportedError(
                    "Levenshtein fuzzy search requires the PostgreSQL fuzzystrmatch extension. "
                    "Enable it by running: python manage.py enable_fuzzystrmatch"
                ) from error
            if "f_unaccent" in error_message or "unaccent" in error_message:
                raise NotSupportedError(
                    "Accent-insensitive fuzzy search requires the unaccent extension and "
                    "f_unaccent() function. Run: python manage.py enable_unaccent"
                ) from error
        raise

    supports_facet = True

    def facet(self, field_name):
        # Get field
        field = self.query_compiler._get_filterable_field(field_name)
        if field is None:
            raise FilterFieldError(
                'Cannot facet search results with field "'
                + field_name
                + "\". Please add index.FilterField('"
                + field_name
                + "') to "
                + self.query_compiler.queryset.model.__name__
                + ".search_fields.",
                field_name=field_name,
            )

        query = self.query_compiler.search(
            self.query_compiler.get_config(self.backend),
            None,
            None,
            backend=self.backend,
        )
        results = (
            query.values(field_name).annotate(count=Count("pk")).order_by("-count")
        )

        return OrderedDict(
            [(result[field_name], result["count"]) for result in results]
        )


class PostgresSearchRebuilder:
    def __init__(self, index):
        self.index = index

    def start(self):
        self.index.delete_stale_entries()
        return self.index

    def finish(self):
        self.index._refresh_title_norms(full=True)


class PostgresSearchAtomicRebuilder(PostgresSearchRebuilder):
    def __init__(self, index):
        super().__init__(index)
        self.transaction = transaction.atomic(using=index.write_connection.alias)
        self.transaction_opened = False

    def start(self):
        self.transaction.__enter__()
        self.transaction_opened = True
        return super().start()

    def finish(self):
        self.index._refresh_title_norms(full=True)

        self.transaction.__exit__(None, None, None)
        self.transaction_opened = False

    def __del__(self):
        # TODO: Implement a cleaner way to close the connection on failure.
        if self.transaction_opened:
            self.transaction.needs_rollback = True
            self.finish()


class PostgresSearchBackend(BaseSearchBackend):
    query_compiler_class = PostgresSearchQueryCompiler
    autocomplete_query_compiler_class = PostgresAutocompleteQueryCompiler
    index_class = PostgresIndex
    results_class = PostgresSearchResults
    rebuilder_class = PostgresSearchRebuilder
    atomic_rebuilder_class = PostgresSearchAtomicRebuilder

    def __init__(self, params):
        super().__init__(params)
        self.config = params.get("SEARCH_CONFIG")

        # Use 'simple' config for autocomplete to disable stemming
        # A good description for why this is important can be found at:
        # https://www.postgresql.org/docs/9.1/datatype-textsearch.html#DATATYPE-TSQUERY
        self.autocomplete_config = "simple"

        # Fuzzy search similarity threshold (0.0 to 1.0)
        # Higher values require closer matches, lower values allow more fuzzy matches
        self.fuzzy_similarity_threshold = params.get(
            "FUZZY_SIMILARITY_THRESHOLD", DEFAULT_FUZZY_SIMILARITY_THRESHOLD
        )

        # Fuzzy search prefix boost - multiplier bonus when field starts with query
        # Set to a value like 0.5-2.0 to prioritize prefix matches
        self.fuzzy_prefix_boost = params.get(
            "FUZZY_PREFIX_BOOST", DEFAULT_FUZZY_PREFIX_BOOST
        )

        # Fuzzy search algorithm: "trigram" (pg_trgm) or "levenshtein" (fuzzystrmatch)
        self.fuzzy_algorithm = params.get("FUZZY_ALGORITHM", DEFAULT_FUZZY_ALGORITHM)

        if params.get("ATOMIC_REBUILD", True):
            self.rebuilder_class = self.atomic_rebuilder_class


SearchBackend = PostgresSearchBackend
