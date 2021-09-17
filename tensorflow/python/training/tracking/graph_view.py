"""Manages a graph of Trackable objects."""
# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import copy
import weakref

from tensorflow.core.protobuf import trackable_object_graph_pb2
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.training import optimizer as optimizer_v1
from tensorflow.python.training.saving import saveable_object_util
from tensorflow.python.training.tracking import base
from tensorflow.python.util import object_identity
from tensorflow.python.util.tf_export import tf_export


_ESCAPE_CHAR = "."  # For avoiding conflicts with user-specified names.

# Keyword for identifying that the next bit of a checkpoint variable name is a
# slot name. Checkpoint names for slot variables look like:
#
#   <path to variable>/<_OPTIMIZER_SLOTS_NAME>/<path to optimizer>/<slot name>
#
# Where <path to variable> is a full path from the checkpoint root to the
# variable being slotted for.
_OPTIMIZER_SLOTS_NAME = _ESCAPE_CHAR + "OPTIMIZER_SLOT"
# Keyword for separating the path to an object from the name of an
# attribute in checkpoint names. Used like:
#   <path to variable>/<_OBJECT_ATTRIBUTES_NAME>/<name of attribute>
_OBJECT_ATTRIBUTES_NAME = _ESCAPE_CHAR + "ATTRIBUTES"

# Factory and related info used to build a SaveableObject that saves a Trackable
# to checkpoint.
_CheckpointFactoryData = collections.namedtuple(
    "_CheckpointFactoryData", ["factory", "name", "checkpoint_key"])


def _escape_local_name(name):
  # We need to support slashes in local names for compatibility, since this
  # naming scheme is being patched in to things like Layer.add_variable where
  # slashes were previously accepted. We also want to use slashes to indicate
  # edges traversed to reach the variable, so we escape forward slashes in
  # names.
  return (name.replace(_ESCAPE_CHAR, _ESCAPE_CHAR + _ESCAPE_CHAR)
          .replace(r"/", _ESCAPE_CHAR + "S"))


def _object_prefix_from_path(path_to_root):
  return "/".join(
      (_escape_local_name(trackable.name)
       for trackable in path_to_root))


def _slot_variable_naming_for_optimizer(optimizer_path):
  """Make a function for naming slot variables in an optimizer."""
  # Name slot variables:
  #
  #   <variable name>/<_OPTIMIZER_SLOTS_NAME>/<optimizer path>/<slot name>
  #
  # where <variable name> is exactly the checkpoint name used for the original
  # variable, including the path from the checkpoint root and the local name in
  # the object which owns it. Note that we only save slot variables if the
  # variable it's slotting for is also being saved.

  optimizer_identifier = "/%s/%s/" % (_OPTIMIZER_SLOTS_NAME, optimizer_path)

  def _name_slot_variable(variable_path, slot_name):
    """With an optimizer specified, name a slot variable."""
    return (variable_path
            + optimizer_identifier
            + _escape_local_name(slot_name))

  return _name_slot_variable


def _serialize_slot_variables(trackable_objects, node_ids, object_names):
  """Gather and name slot variables."""
  non_slot_objects = list(trackable_objects)
  slot_variables = object_identity.ObjectIdentityDictionary()
  for trackable in non_slot_objects:
    if (isinstance(trackable, optimizer_v1.Optimizer)
        # TODO(b/110718070): Fix Keras imports.
        # Note: dir() is used rather than hasattr() here to avoid triggering
        # custom __getattr__ code, see b/152031870 for context.
        or "_create_or_restore_slot_variable" in dir(trackable)):
      naming_scheme = _slot_variable_naming_for_optimizer(
          optimizer_path=object_names[trackable])
      slot_names = trackable.get_slot_names()
      for slot_name in slot_names:
        for original_variable_node_id, original_variable in enumerate(
            non_slot_objects):
          try:
            slot_variable = trackable.get_slot(
                original_variable, slot_name)
          except (AttributeError, KeyError):
            slot_variable = None
          if slot_variable is None:
            continue
          slot_variable._maybe_initialize_trackable()  # pylint: disable=protected-access
          if slot_variable._checkpoint_dependencies:  # pylint: disable=protected-access
            # TODO(allenl): Gather dependencies of slot variables.
            raise NotImplementedError(
                "Currently only variables with no dependencies can be saved as "
                "slot variables. File a feature request if this limitation "
                "bothers you.")
          if slot_variable in node_ids:
            raise NotImplementedError(
                ("A slot variable was re-used as a dependency of a "
                 "Trackable object: %s. This is not currently "
                 "allowed. File a feature request if this limitation bothers "
                 "you.") % slot_variable)
          checkpoint_name = naming_scheme(
              variable_path=object_names[original_variable],
              slot_name=slot_name)
          object_names[slot_variable] = checkpoint_name
          slot_variable_node_id = len(trackable_objects)
          node_ids[slot_variable] = slot_variable_node_id
          trackable_objects.append(slot_variable)
          slot_variable_proto = (
              trackable_object_graph_pb2.TrackableObjectGraph
              .TrackableObject.SlotVariableReference(
                  slot_name=slot_name,
                  original_variable_node_id=original_variable_node_id,
                  slot_variable_node_id=slot_variable_node_id))
          slot_variables.setdefault(trackable, []).append(
              slot_variable_proto)
  return slot_variables


def get_checkpoint_factories_and_keys(object_names, for_saved_model=False):
  """Gets a map of saveable factories and corresponding checkpoint keys.

  Args:
    object_names: a dictionary that maps `Trackable` objects to auto-generated
      string names.
    for_saved_model: Whether to gather the Saveables for the SavedModel
      GraphDef.
  Returns:
    A dictionary mapping Trackables -> a list of _CheckpointFactoryData.
  """
  checkpoint_factory_map = object_identity.ObjectIdentityDictionary()
  for trackable, object_name in object_names.items():
    checkpoint_factory_map[trackable] = []
    for name, saveable_factory in (
        _gather_saveables(trackable, for_saved_model).items()):
      checkpoint_key = "%s/%s/%s" % (
          object_name, _OBJECT_ATTRIBUTES_NAME, _escape_local_name(name))
      checkpoint_factory_map[trackable].append(_CheckpointFactoryData(
          factory=saveable_factory,
          name=name,
          checkpoint_key=checkpoint_key))
  return checkpoint_factory_map


def _gather_saveables(trackable, for_saved_model):
  """Returns a dictionary of Saveables that define the save/restore ops."""
  if for_saved_model:
    return trackable._gather_saveables_for_saved_model()  # pylint: disable=protected-access
  else:
    return trackable._gather_saveables_for_checkpoint()  # pylint: disable=protected-access


@tf_export("__internal__.tracking.ObjectGraphView", v1=[])
class ObjectGraphView(object):
  """Gathers and serializes an object graph."""

  def __init__(self, root, saveables_cache=None, attached_dependencies=None):
    """Configure the graph view.

    Args:
      root: A `Trackable` object whose variables (including the variables
        of dependencies, recursively) should be saved. May be a weak reference.
      saveables_cache: A dictionary mapping `Trackable` objects ->
        attribute names -> SaveableObjects, used to avoid re-creating
        SaveableObjects when graph building.
      attached_dependencies: Dependencies to attach to the root object. Used
        when saving a Checkpoint with a defined root object.
    """
    self._root_ref = root
    self._saveables_cache = saveables_cache
    self._attached_dependencies = attached_dependencies

  def __deepcopy__(self, memo):
    if isinstance(self._root_ref, weakref.ref):
      # By default, weak references are not copied, which leads to surprising
      # deepcopy behavior. To fix, we first we copy the object itself, then we
      # make a weak reference to the copy.
      strong_root = self._root_ref()
      if strong_root is not None:
        strong_copy = copy.deepcopy(strong_root, memo)
        memo[id(self._root_ref)] = weakref.ref(strong_copy)
    # super() does not have a __deepcopy__, so we need to re-implement it
    copied = super().__new__(type(self))
    memo[id(self)] = copied
    for key, value in vars(self).items():
      setattr(copied, key, copy.deepcopy(value, memo))
    return copied

  def list_dependencies(self, obj):
    # pylint: disable=protected-access
    obj._maybe_initialize_trackable()
    dependencies = obj._checkpoint_dependencies
    # pylint: enable=protected-access

    if obj is self.root and self._attached_dependencies:
      dependencies = dependencies.copy()
      dependencies.extend(self._attached_dependencies)
    return dependencies

  @property
  def saveables_cache(self):
    """Maps Trackable objects -> attribute names -> list(SaveableObjects).

    Used to avoid re-creating SaveableObjects when graph building. None when
    executing eagerly.

    Returns:
      The cache (an object-identity dictionary), or None if caching is disabled.
    """
    return self._saveables_cache

  @property
  def attached_dependencies(self):
    """Returns list of dependencies that should be saved in the checkpoint.

    These dependencies are not tracked by root, but are in the checkpoint.
    This is defined when the user creates a Checkpoint with both root and kwargs
    set.

    Returns:
      A list of TrackableReferences.
    """
    return self._attached_dependencies

  @property
  def root(self):
    if isinstance(self._root_ref, weakref.ref):
      derefed = self._root_ref()
      assert derefed is not None
      return derefed
    else:
      return self._root_ref

  def _breadth_first_traversal(self):
    """Find shortest paths to all dependencies of self.root."""
    bfs_sorted = []
    to_visit = collections.deque([self.root])
    path_to_root = object_identity.ObjectIdentityDictionary()
    path_to_root[self.root] = ()
    while to_visit:
      current_trackable = to_visit.popleft()
      bfs_sorted.append(current_trackable)
      for name, dependency in self.list_dependencies(current_trackable):
        if dependency not in path_to_root:
          path_to_root[dependency] = (
              path_to_root[current_trackable] + (
                  base.TrackableReference(name, dependency),))
          to_visit.append(dependency)
    return bfs_sorted, path_to_root

  def _add_attributes_to_object_graph(
      self, trackable_objects, object_graph_proto, node_ids, object_names):
    """Create SaveableObjects and corresponding SerializedTensor protos."""
    named_saveable_objects = []
    if self._saveables_cache is None:
      # No SaveableObject caching. Either we're executing eagerly, or building a
      # static save which is specialized to the current Python state.
      feed_additions = None
    else:
      # If we are caching SaveableObjects, we need to build up a feed_dict with
      # functions computing volatile Python state to be saved with the
      # checkpoint.
      feed_additions = {}
    checkpoint_factory_map = get_checkpoint_factories_and_keys(object_names)
    for checkpoint_id, (trackable, object_proto) in enumerate(
        zip(trackable_objects, object_graph_proto.nodes)):
      assert node_ids[trackable] == checkpoint_id
      if self._saveables_cache is not None:
        cached_attributes = self._saveables_cache.setdefault(trackable, {})
      else:
        cached_attributes = None
      for factory_data in checkpoint_factory_map[trackable]:
        attribute = object_proto.attributes.add()
        attribute.name = name = factory_data.name
        attribute.checkpoint_key = key = factory_data.checkpoint_key
        saveable_factory = factory_data.factory

        # See if we can skip saving this checkpoint key.
        saveables = cached_attributes.get(name) if cached_attributes else None
        if saveables is not None:
          for saveable in saveables:
            if key not in saveable.name:
              # The checkpoint key for this SaveableObject is different. We
              # need to re-create it.
              saveables = None
              del cached_attributes[name]
              break

        if saveables is None:
          saveables = saveable_object_util.create_saveables_from_factory(
              saveable_factory, key)
          for saveable in saveables:
            if key not in saveable.name:
              raise AssertionError(
                  f"The object {trackable} produced a SaveableObject with name "
                  f"'{saveable.name}' for attribute '{name}'. Expected a name"
                  f" containing '{key}'.")
          if cached_attributes is not None:
            cached_attributes[name] = saveables

        optional_restore = None
        for saveable in saveables:
          if optional_restore is None:
            optional_restore = saveable.optional_restore
          else:
            optional_restore = optional_restore and saveable.optional_restore

          if hasattr(saveable, "full_name"):
            attribute.full_name = saveable.full_name
          if isinstance(saveable, base.PythonStateSaveable):
            if feed_additions is None:
              assert self._saveables_cache is None
              # If we're not caching saveables, then we're either executing
              # eagerly or building a static save/restore (e.g. for a
              # SavedModel). In either case, we should embed the current Python
              # state in the graph rather than relying on a feed dict.
              saveable = saveable.freeze()
            else:
              saveable_feed_dict = saveable.feed_dict_additions()
              for new_feed_key in saveable_feed_dict.keys():
                if new_feed_key in feed_additions:
                  raise AssertionError(
                      ("The object %s tried to feed a value for the Tensor %s "
                       "when saving, but another object is already feeding a "
                       "value.")
                      % (trackable, new_feed_key))
              feed_additions.update(saveable_feed_dict)
          named_saveable_objects.append(saveable)
        if optional_restore is None:
          optional_restore = False
        attribute.optional_restore = optional_restore

    return named_saveable_objects, feed_additions

  def _fill_object_graph_proto(self, trackable_objects,
                               node_ids,
                               slot_variables,
                               object_graph_proto=None):
    """Name non-slot `Trackable`s and add them to `object_graph_proto`."""
    if object_graph_proto is None:
      object_graph_proto = (
          trackable_object_graph_pb2.TrackableObjectGraph())
    for checkpoint_id, trackable in enumerate(trackable_objects):
      assert node_ids[trackable] == checkpoint_id
      object_proto = object_graph_proto.nodes.add()
      object_proto.slot_variables.extend(slot_variables.get(trackable, ()))
      for child in self.list_dependencies(trackable):
        child_proto = object_proto.children.add()
        child_proto.node_id = node_ids[child.ref]
        child_proto.local_name = child.name
    return object_graph_proto

  def serialize_object_graph(self):
    """Determine checkpoint keys for variables and build a serialized graph.

    Non-slot variables are keyed based on a shortest path from the root saveable
    to the object which owns the variable (i.e. the one which called
    `Trackable._add_variable` to create it).

    Slot variables are keyed based on a shortest path to the variable being
    slotted for, a shortest path to their optimizer, and the slot name.

    Returns:
      A tuple of (named_variables, object_graph_proto, feed_additions):
        named_variables: A dictionary mapping names to variable objects.
        object_graph_proto: A TrackableObjectGraph protocol buffer
          containing the serialized object graph and variable references.
        feed_additions: A dictionary mapping from Tensors to values which should
          be fed when saving.

    Raises:
      ValueError: If there are invalid characters in an optimizer's slot names.
    """
    (trackable_objects, unused_path_to_root, node_ids, slot_variables,
     object_names) = self.objects_ids_and_slot_variables_and_paths()
    object_graph_proto = self._fill_object_graph_proto(
        trackable_objects=trackable_objects,
        node_ids=node_ids,
        slot_variables=slot_variables)
    named_saveable_objects, feed_additions = (
        self._add_attributes_to_object_graph(
            trackable_objects=trackable_objects,
            object_graph_proto=object_graph_proto,
            node_ids=node_ids,
            object_names=object_names))
    return named_saveable_objects, object_graph_proto, feed_additions

  def frozen_saveable_objects(self):
    """Creates SaveableObjects with the current object graph frozen."""
    named_saveable_objects, graph_proto, _ = self.serialize_object_graph()
    with ops.device("/cpu:0"):
      object_graph_tensor = constant_op.constant(
          graph_proto.SerializeToString(), dtype=dtypes.string)
    named_saveable_objects.append(
        base.NoRestoreSaveable(
            tensor=object_graph_tensor,
            name=base.OBJECT_GRAPH_PROTO_KEY))
    return named_saveable_objects

  def objects_ids_and_slot_variables_and_paths(self):
    """Traverse the object graph and list all accessible objects.

    Looks for `Trackable` objects which are dependencies of
    `root_trackable`. Includes slot variables only if the variable they are
    slotting for and the optimizer are dependencies of `root_trackable`
    (i.e. if they would be saved with a checkpoint).

    Returns:
      A tuple of (trackable objects, paths from root for each object,
                  object -> node id, slot variables)
    """
    trackable_objects, path_to_root = self._breadth_first_traversal()
    object_names = object_identity.ObjectIdentityDictionary()
    for obj, path in path_to_root.items():
      object_names[obj] = _object_prefix_from_path(path)
    node_ids = object_identity.ObjectIdentityDictionary()
    for node_id, node in enumerate(trackable_objects):
      node_ids[node] = node_id
    slot_variables = _serialize_slot_variables(
        trackable_objects=trackable_objects,
        node_ids=node_ids,
        object_names=object_names)
    return (trackable_objects, path_to_root, node_ids, slot_variables,
            object_names)

  def objects_ids_and_slot_variables(self):
    trackable_objects, _, node_ids, slot_variables, _ = (
        self.objects_ids_and_slot_variables_and_paths())
    return trackable_objects, node_ids, slot_variables

  def list_objects(self):
    """Traverse the object graph and list all accessible objects."""
    trackable_objects, _, _ = self.objects_ids_and_slot_variables()
    return trackable_objects
