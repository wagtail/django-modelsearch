from django.db.models.signals import post_delete, post_save

from . import index
from .tasks import insert_or_update_object_task, mp_tree_path_updated_task


try:
    from treebeard.mp_tree import MP_Node, path_updated
except ImportError:
    MP_Node = None


try:
    from treebeard.ns_tree import (
        NS_Node,
        gap_altered,
        subtree_moved,
        tree_ids_incremented,
    )
except ImportError:
    NS_Node = None


def post_save_signal_handler(instance, **kwargs):
    if kwargs.get("raw", False):
        return

    insert_or_update_object_task.enqueue(
        instance._meta.app_label, instance._meta.model_name, str(instance.pk)
    )


def post_delete_signal_handler(instance, **kwargs):
    index.remove_object(instance)


def indexes_with_method(model, method_name):
    """
    Returns an iterator of (backend_name, index) tuples for all auto-updating indexes for the given
    model that have the given method
    """
    for backend_name, backend in index.get_search_backends_with_name(
        with_auto_update=True
    ):
        index_obj = backend.get_index_for_model(model)
        if hasattr(index_obj, method_name):
            yield (backend_name, index_obj)


def mp_tree_path_updated_signal_handler(sender, old_path, new_path, **kwargs):
    for backend_name, _index in indexes_with_method(
        sender, "process_mptree_path_updated"
    ):
        mp_tree_path_updated_task.enqueue(
            backend_name,
            sender._meta.app_label,
            sender._meta.model_name,
            old_path,
            new_path,
        )


def ns_tree_gap_altered_signal_handler(sender, tree_id, start_index, offset, **kwargs):
    # Do not enqueue this as an asynchronous task, because an NS_Node move operation involves multiple signals
    # that need to be processed in order
    for _backend_name, index_obj in indexes_with_method(
        sender, "process_nstree_gap_altered"
    ):
        index_obj.process_nstree_gap_altered(sender, tree_id, start_index, offset)


def ns_tree_subtree_moved_signal_handler(
    sender, tree_id, lft, rgt, target_tree_id, index_offset, depth_offset, **kwargs
):
    # Do not enqueue this as an asynchronous task, because an NS_Node move operation involves multiple signals
    # that need to be processed in order
    for _backend_name, index_obj in indexes_with_method(
        sender, "process_nstree_subtree_moved"
    ):
        index_obj.process_nstree_subtree_moved(
            sender, tree_id, lft, rgt, target_tree_id, index_offset, depth_offset
        )


def ns_tree_tree_ids_incremented_signal_handler(sender, min_tree_id, **kwargs):
    # Do not enqueue this as an asynchronous task, because an NS_Node move operation involves multiple signals
    # that need to be processed in order
    for _backend_name, index_obj in indexes_with_method(
        sender, "process_nstree_tree_ids_incremented"
    ):
        index_obj.process_nstree_tree_ids_incremented(sender, min_tree_id)


def register_signal_handlers():
    # Loop through list and register signal handlers for each one
    for model in index.get_indexed_models():
        if not getattr(model, "search_auto_update", True):
            continue

        post_save.connect(post_save_signal_handler, sender=model)
        post_delete.connect(post_delete_signal_handler, sender=model)

        if MP_Node and issubclass(model, MP_Node):
            path_updated.connect(mp_tree_path_updated_signal_handler, sender=model)

        if NS_Node and issubclass(model, NS_Node):
            gap_altered.connect(ns_tree_gap_altered_signal_handler, sender=model)
            subtree_moved.connect(ns_tree_subtree_moved_signal_handler, sender=model)
            tree_ids_incremented.connect(
                ns_tree_tree_ids_incremented_signal_handler, sender=model
            )
