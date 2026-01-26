(modelsearch_searching)=

# Searching

(modelsearch_searching_pages)=

## Searching QuerySets

Django Modelsearch is built on Django's [QuerySet API](https://docs.djangoproject.com/en/stable/ref/models/querysets/). You should be able to search any Django QuerySet provided the model and the fields being filtered have been added to the search index.

### Making a model searchable

To make an indexed model searchable, you need to create a QuerySet that inherits from `modelsearch.queryset.SearchableQuerySetMixin` and use it on the model's `objects` attribute. This will add the `.search()` method to all QuerySets for the model:

```python
class PersonQuerySet(SearchableQuerySetMixin, QuerySet):
    pass


class Person(index.Indexed, models.Model):
    # ...

    objects = PersonQuerySet.as_manager()

```

### Simple searches

To search the model by all searchable fields:

```python
Person.objects.search("Stefan Zweig")
```

Pick individual fields to search on with the `fields` argument:

```python
Person.objects.search("Stefan", fields=["first_name"])
```

Filter results by using Django's `.filter()` and `.exclude()` methods before `.search()`

```python
Person.objects.filter(birth_date__year=1881).search(...)
```

*Note that any fields you filter on must be indexed with `index.FilterField`.*

If you've indexed a `ForeignKey` or `OneToOneField`, you can search using the related name (assuming the `Book` model is also indexed in this example):

```python
stefan_zweig.books.search("The World of Yesterday")
```

(fuzzy_matching)=

#### Fuzzy search

Fuzzy search can help autocorrect misspelled queries:

```python
from modelsearch.query import Fuzzy

james_joyce.books.search(Fuzzy("Ulises"))
```

#### Phrase search

Search queries usually allow the terms to be a different order and not next to each other in the document. You can use a phrase search if you need the words to appear in their exact order:

```python
from modelsearch.query import Phrase

Book.objects.search(Phrase("Peace and War"))
```

#### Custom ordering

Pass an `order_by` argument to `search()` to order by that field. This will disable ranking and just do a basic match search:

```python
Book.objects.search("The Hobbit", order_by="release_date")
```

Note the field being ordered by must be indexed with `index.FilterField`.

Alternatively, passing `order_by_relevance=False` when calling `search` will preserve any ordering already defined on the queryset:

```python
Book.objects.order_by('release_date').search("The Hobbit", order_by_relevance=False)
```


#### Changing the search operator

The search operator determines whether we need to match all terms in the query or just one:

- `AND` - Match all terms
- `OR` - Match one or more terms

By default, Django Modelsearch searches with the `OR` operator. This helps if the user misspells a word in the query. Search ranking will ensure the best match always gets to the top.

But if you don't want records that don't match the whole query to appear, you can switch to the `AND` operator instead:

```python
Book.objects.get("The tale of two cities", operator="and")
```

We won't get random books about tales and cities in the results, just the Charles Dickens classic.

### Autocomplete search

Autocomplete searches are a bit special because they require different indexing behaviour. This is for performance but also to switch off stemming which can cause strange results with autocomplete.

Also, generally, you only want to autocomplete names and titles of things and not the contents.

For these reasons, Django Modelsearch indexes them separately to search fields. Any field that you want to autocomplete on must also be indexed with `index.AutocompleteField`.

To run an autocomplete query, use the `.autocomplete()` method:

```python
Person.objects.autocomplete('stef')
```

### Structured Queries

In the above section, we saw a couple of query objects: `Fuzzy` and `Phrase`.

There are a couple more:

- `PlainText(query, operator='or')` - This is the default one
- `Boost(query, boost)` - Boosts the wrapped query

Query objects can be combined with `|` and `&` operators. parentheses can be used as well to build complex structured queries:

```python
Book.objects.search(Boost(Phrase("War and Peace"), 2.0) | PlainText("War and Peace"))
```

This will perform both a phrase and a plain search and give an extra boost to results that match the phrase too.

### How does `.search()` work?

When you call `.search()` on a QuerySet, it is converted to a SearchResults object. Any filters or ordering that was applied on the QuerySet are translated and applied to the new SearchResults.

Like with QuerySets, the search is not actually performed until you try to iterate the results or fetch an individual result.

### SearchResults methods

The SearchResults class has a couple of useful methods:

(modelsearch_faceted_search)=

#### `facet(field_name)`

Performs a faceted search on the results. It returns a dictionary containing each value of the given field as keys, and the counts of records as values.

For example, say we are searching for products, and we want to see the categories faceted:

```python
>>> Product.objects.search("The Hobbit").facet("category")
{
    "Books": 3,
    "Films": 1,
    "Games": 5,
}
```

(modelsearch_annotating_results_with_score)=

#### `annotate_score(field_name)`

Search engines work by calculating a score for each result and ordering the results by that score.

This method allows you to see the score for each result by annotating it on each returned object. This is useful for debugging:

```python
>>> results = Product.objects.search("The Hobbit").annotate_score("score")
>>> results[0].score
123.4
```

### Query string parser

Modelsearch provides a little helper for parsing a well known syntax for phrase queries (`"double quotes"`) and filters (`field:value`) into a query object and a `QueryDict` of filters (the same type Django uses for `request.GET`):

```python
from modelsearch.utils import parse_query_string

filters, query = parse_query_string('my query string "this is a phrase" this_is_a:filter key:value1 key:value2')

filters == {
    'this_is_a': ['filter'],
    'key': ['value1', 'value2']
}

query == PlainText("my query string") & Phrase("this is a phrase")
```
