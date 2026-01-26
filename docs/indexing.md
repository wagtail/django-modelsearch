(modelsearch_indexing)=

# Indexing

To make data searchable, we need to load it into a search index. Django modelsearch provides a way to define an index mapping on the model which can used by Elasticsearch to determine what fields are searcahble, how they should be analysed, etc.

## Quick example

Let's start with the simple example from the [Django docs](https://docs.djangoproject.com/en/5.2/topics/db/models/#quick-example). To make the model searchable, we need to make it inherit from `Indexed` and define a list of `search_fields`:

```python
from django.db import models
from modelsearch import index

class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)

    search_fields = [
        index.SearchField('first_name'),
        index.SearchField('last_name'),
    ]
```

When you run `rebuild_modelsearch_index`, the indexer will find this model because it inherits from the `Indexed` class and then index all of its records by its `search_fields`. When you then try to search this model, the search query will be matched against those two fields.

## Boosting search fields

You can define a boost on `index.SearchField` to apply index-time boosting to that field.
This allows you to rank matches in important fields (such as a name or title) higher than less important fields.

When boosting is applied, the scores of matches to that field will be multiplied by the boost.

For example, let's add a biography field and boost the name fields:

```python
class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)
    biography = models.TextField()

    search_fields = [
        index.SearchField('first_name', boost=2.0),
        index.SearchField('last_name', boost=2.0),
        index.SearchField('biography'),
    ]
```

This will make sure direct name matches will always out-rank mentions of the person in other people's biographies.

## Indexing callables

You can also pass callable methods to `index.SearchField` which can be helpful for cleaning up text for the search engine:

```python
class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)
    biography_html = models.TextField()

    def biography_plain(self):
        # Strip HTML tags from biography
        return strip_html(self.biography_html)

    search_fields = [
        index.SearchField('first_name'),
        index.SearchField('last_name'),
        index.SearchField('biography_plain'),
    ]
```

## Indexing filterable fields

We may also want to filter our results on a field while searching. Because modelsearch supports indexing in separate search engines (like Elasticsearch), we need to also index any filterable fields so that the filters can be applied with the search query.

To do this, index the field with `index.FilterField`, for example, let's add a birth date:

```python
class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)
    birth_date = models.DateField()

    search_fields = [
        index.SearchField('first_name'),
        index.SearchField('last_name'),
        index.FilterField('birth_date'),
    ]
```

Note that if you want to both search and filter on a field, you will need to index it as with both `index.SearchField` and `index.FilterField`.

## Indexing related content

You can also pull in related content from models that are linked with a `ForeignKey`, `OneToOneField`, or `ManyToManyField`, both forwards and reverse.

This is done using `index.RelatedFields` which itself takes a list of fields.

For example, if we had a book model with a link to an author, we can index the author details with the book:

```python
class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)

    search_fields = [
        index.SearchField('first_name'),
        index.SearchField('last_name'),
    ]


class Book(index.Indexed, models.Model):
    title = models.CharField(max_length=255)
    author = models.ForeignKey(Author, on_delete=models.SET_NULL, related_name='books')

    search_fields = [
        index.SearchField('first_name'),
        # You can provide a different list of fields, or reference the model's search_fields
        index.RelatedFields('author', Person.search_fields),
    ]
```

Now when you search for books with an author's name, books with that author will show up.

## Indexing models with multi-table-inheritance

Django supports creating models that inherit from other concrete models. This is known as [multi-table-inheritance](https://docs.djangoproject.com/en/5.2/topics/db/models/#multi-table-inheritance).

Django ModelSearch supports multi-table-inheritance by putting all models with the same root in the same index. For example, let's say we have these models:

- Person(Model)
- Pet(Model)
- Dog(Pet)
- Cat(Pet)

There will be two indexes created for this, Person and Pet.

To prevent name clashes, fields on the Dog and Cat models that are not on the Pet model will be prefixed with `dog_` and `cat_` respectively.

When searching the whole Pet index, all `SearchField`s will be searched, including those defined on Dog And Cat.

### Implementing `get_indexed_instance`

Django by itself has no way of telling us that a given instance of the root model (Pet) also has an instance of the (Dog). This is a problem because if a field on the root model is altered, Django ModelSearch will reindex it without the fields on the child model.

To resolve this, you can implement `get_indexed_instance()` on the root model. If the model has a child model instance, it should return the child. For example:

```python
from django.db import models


class Pet(models.Model):
    name = models.TextField()
    species = models.TextField()

    def get_indexed_instance(self):
        if self.species == "cat":
            return self.cat
        elif self.species == "dog":
            return self.dog
        else:
            return self


class Cat(Pet):
    pass


class Dog(Pet)
    pass
```

Django ModelSearch will always call `get_indexed_instance` before indexing to get the most specific version of the object to index.
