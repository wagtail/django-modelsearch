from django.db.models.signals import post_delete, post_save

from . import index
from .tasks import insert_or_update_object_task, mp_tree_path_updated_task


try:
    from treebeard.mp_tree import MP_Node, path_updated
except ImportError:
    MP_Node = None
    path_updated = None


def post_save_signal_handler(instance, **kwargs):
    if kwargs.get("raw", False):
        return

    insert_or_update_object_task.enqueue(
        instance._meta.app_label, instance._meta.model_name, str(instance.pk)
    )


def post_delete_signal_handler(instance, **kwargs):
    index.remove_object(instance)


def mp_tree_path_updated_signal_handler(sender, old_path, new_path, **kwargs):
    mp_tree_path_updated_task.enqueue(
        sender._meta.app_label, sender._meta.model_name, old_path, new_path
    )


def register_signal_handlers():
    # Loop through list and register signal handlers for each one
    for model in index.get_indexed_models():
        if not getattr(model, "search_auto_update", True):
            continue

        post_save.connect(post_save_signal_handler, sender=model)
        post_delete.connect(post_delete_signal_handler, sender=model)

        if MP_Node and issubclass(model, MP_Node):
            # See if any registered auto-updating backends support path updates
            should_register_path_updated_signal = False
            for _backend_name, backend in index.get_search_backends_with_name(
                with_auto_update=True
            ):
                index_obj = backend.get_index_for_model(model)
                if hasattr(index_obj, "process_mptree_path_updated"):
                    should_register_path_updated_signal = True
                    break

            if should_register_path_updated_signal:
                path_updated.connect(mp_tree_path_updated_signal_handler, sender=model)
