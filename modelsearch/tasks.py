from django.apps import apps
from django_tasks import task

from modelsearch import index
from modelsearch.backends import get_search_backend


@task()
def insert_or_update_object_task(app_label, model_name, pk):
    model = apps.get_model(app_label, model_name)
    index.insert_or_update_object(model.objects.get(pk=pk))


@task()
def mp_tree_path_updated_task(backend_name, app_label, model_name, old_path, new_path):
    model = apps.get_model(app_label, model_name)
    backend = get_search_backend(backend_name)
    index_obj = backend.get_index_for_model(model)
    if hasattr(index_obj, "process_mptree_path_updated"):
        index_obj.process_mptree_path_updated(model, old_path, new_path)
