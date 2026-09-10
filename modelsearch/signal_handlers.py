import logging

from django.db.models.signals import post_delete, post_save

from . import index
from .tasks import insert_or_update_object_task


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


logger = logging.getLogger("modelsearch.index")


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
    Returns an iterator of (backend_name, backend, index) tuples for all auto-updating indexes for
    the given model that have the given method
    """
    for backend_name, backend in index.get_search_backends_with_name(
        with_auto_update=True
    ):
        index_obj = backend.get_index_for_model(model)
        if hasattr(index_obj, method_name):
            yield (backend_name, backend, index_obj)


# Signal handlers for treebeard signals emitted during tree move operations. These are handled
# in-process rather than offloaded to a task queue, because they often involve multiple signals
# that need to be processed in order.


def mp_tree_path_updated_signal_handler(sender, old_path, new_path, **kwargs):
    for _backend_name, backend, index_obj in indexes_with_method(
        sender, "process_mptree_path_updated"
    ):
        try:
            index_obj.process_mptree_path_updated(sender, old_path, new_path)
        except Exception as e:
            logger.exception(
                "Error processing mptree.path_updated signal for model %s: %s",
                sender.__name__,
                e,
            )
            if not backend.catch_indexing_errors:
                raise


def ns_tree_gap_altered_signal_handler(sender, tree_id, start_index, offset, **kwargs):
    for _backend_name, backend, index_obj in indexes_with_method(
        sender, "process_nstree_gap_altered"
    ):
        try:
            index_obj.process_nstree_gap_altered(sender, tree_id, start_index, offset)
        except Exception as e:
            logger.exception(
                "Error processing nstree.gap_altered signal for model %s: %s",
                sender.__name__,
                e,
            )
            if not backend.catch_indexing_errors:
                raise


def ns_tree_subtree_moved_signal_handler(
    sender, tree_id, lft, rgt, target_tree_id, index_offset, depth_offset, **kwargs
):
    for _backend_name, backend, index_obj in indexes_with_method(
        sender, "process_nstree_subtree_moved"
    ):
        try:
            index_obj.process_nstree_subtree_moved(
                sender, tree_id, lft, rgt, target_tree_id, index_offset, depth_offset
            )
        except Exception as e:
            logger.exception(
                "Error processing nstree.subtree_moved signal for model %s: %s",
                sender.__name__,
                e,
            )
            if not backend.catch_indexing_errors:
                raise


def ns_tree_tree_ids_incremented_signal_handler(sender, min_tree_id, **kwargs):
    for _backend_name, backend, index_obj in indexes_with_method(
        sender, "process_nstree_tree_ids_incremented"
    ):
        try:
            index_obj.process_nstree_tree_ids_incremented(sender, min_tree_id)
        except Exception as e:
            logger.exception(
                "Error processing nstree.tree_ids_incremented signal for model %s: %s",
                sender.__name__,
                e,
            )
            if not backend.catch_indexing_errors:
                raise


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
