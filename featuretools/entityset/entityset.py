import copy
import itertools
import logging
from builtins import object, range, zip
from collections import defaultdict

import cloudpickle
import numpy as np
import pandas as pd
from pandas.api.types import is_dtype_equal, is_numeric_dtype

from .entity import Entity
from .relationship import Relationship
from .serialization import load_entity_data, write_entityset

import featuretools.variable_types.variable as vtypes
from featuretools.utils.gen_utils import make_tqdm_iterator

pd.options.mode.chained_assignment = None  # default='warn'
logger = logging.getLogger('featuretools.entityset')


class BFSNode(object):

    def __init__(self, entity_id, parent, relationship):
        self.entity_id = entity_id
        self.parent = parent
        self.relationship = relationship

    def build_path(self):
        path = []
        cur_node = self
        num_forward = 0
        i = 0
        last_forward = False
        while cur_node.parent is not None:
            path.append(cur_node.relationship)
            if cur_node.relationship.parent_entity.id == cur_node.entity_id:
                num_forward += 1
                if i == 0:
                    last_forward = True
            cur_node = cur_node.parent
            i += 1
        path.reverse()

        # if path ends on a forward relationship, return number of
        # forward relationships, otherwise 0
        if len(path) == 0 or not last_forward:
            num_forward = 0
        return path, num_forward


class EntitySet(object):
    """
    Stores all actual data for a entityset

    Attributes:
        id
        entity_dict
        relationships
        time_type

    Properties:
        metadata

    """
    id = None
    entities = []
    relationships = []
    name = None

    def __init__(self, id, entities=None, relationships=None):
        """Creates EntitySet

            Args:
                id (str) : Unique identifier to associate with this instance

                entities (dict[str -> tuple(pd.DataFrame, str, str)]): Dictionary of
                    entities. Entries take the format
                    {entity id -> (dataframe, id column, (time_column), (variable_types))}.
                    Note that time_column and variable_types are optional.

                relationships (list[(str, str, str, str)]): List of relationships
                    between entities. List items are a tuple with the format
                    (parent entity id, parent variable, child entity id, child variable).

            Example:

                .. code-block:: python

                    entities = {
                        "cards" : (card_df, "id"),
                        "transactions" : (transactions_df, "id", "transaction_time")
                    }

                    relationships = [("cards", "id", "transactions", "card_id")]

                    ft.EntitySet("my-entity-set", entities, relationships)
        """
        self.id = id
        self.entity_dict = {}
        self.relationships = []
        self.time_type = None

        entities = entities or {}
        relationships = relationships or []
        for entity in entities:
            df = entities[entity][0]
            index_column = entities[entity][1]
            time_column = None
            variable_types = None
            if len(entities[entity]) > 2:
                time_column = entities[entity][2]
            if len(entities[entity]) > 3:
                variable_types = entities[entity][3]
            self.entity_from_dataframe(entity_id=entity,
                                       dataframe=df,
                                       index=index_column,
                                       time_index=time_column,
                                       variable_types=variable_types)

        for relationship in relationships:
            parent_variable = self[relationship[0]][relationship[1]]
            child_variable = self[relationship[2]][relationship[3]]
            self.add_relationship(Relationship(parent_variable,
                                               child_variable))
        self.reset_metadata()

    def __sizeof__(self):
        return sum([entity.__sizeof__() for entity in self.entities])

    def __dask_tokenize__(self):
        return (EntitySet, cloudpickle.dumps(self.metadata))

    def __eq__(self, other, deep=False):
        if len(self.entity_dict) != len(other.entity_dict):
            return False
        for eid, e in self.entity_dict.items():
            if eid not in other.entity_dict:
                return False
            if not e.__eq__(other[eid], deep=deep):
                return False
        for r in other.relationships:
            if r not in other.relationships:
                return False
        return True

    def __ne__(self, other, deep=False):
        return not self.__eq__(other, deep=deep)

    def __getitem__(self, entity_id):
        """Get entity instance from entityset

        Args:
            entity_id (str): Id of entity.

        Returns:
            :class:`.Entity` : Instance of entity. None if entity doesn't
                exist.

        Example:
            >>> my_entityset[entity_id]
            <Entity: id>
        """
        if entity_id in self.entity_dict:
            return self.entity_dict[entity_id]
        raise KeyError('Entity %s does not exist in %s' % (entity_id, self.id))

    @property
    def entities(self):
        return list(self.entity_dict.values())

    @property
    def metadata(self):
        '''Version of this EntitySet with all data replaced with empty DataFrames.

        An EntitySet's metadata is used in many places, for instance,
        for each feature in a feature list.
        To prevent using copying the full metadata object to each feature,
        we generate a new metadata object and check if it's the same as the existing one,
        and if it is return the existing one. Thus, all features in the feature list
        would reference the same object, rather than copies. This saves a lot of memory
        '''
        if self._metadata is None:
            self._metadata = self.from_metadata(self.create_metadata_dict(),
                                                data_root=None)

        return self._metadata

    def reset_metadata(self):
        self._metadata = None

    @property
    def is_metadata(self):
        '''Returns True if all of the Entity's contain no data (empty DataFrames).
        In general, EntitySets with no data are created by accessing the EntitySet.metadata property,
        which returns a copy of the current EntitySet with all data removed.
        '''
        return all(e.df.empty for e in self.entity_dict.values())

    def to_pickle(self, path):
        '''Write entityset to disk in the pickle format, location specified by `path`.

            Args:
                * entityset: entityset to write to disk
                * path (str): location on disk to write to (will be created as a directory)
        '''

        write_entityset(self, path, serialization_method='pickle')
        return self

    def to_parquet(self, path):
        '''Write entityset to disk in the parquet format, location specified by `path`.

            Args:
                * entityset: entityset to write to disk
                * path (str): location on disk to write to (will be created as a directory)
        '''

        write_entityset(self, path, serialization_method='parquet')
        return self

    def create_metadata_dict(self):
        return {
            'id': self.id,
            'relationships': [{
                'parent_entity': r.parent_entity.id,
                'parent_variable': r.parent_variable.id,
                'child_entity': r.child_entity.id,
                'child_variable': r.child_variable.id,
            } for r in self.relationships],
            'entity_dict': {eid: {
                'index': e.index,
                'time_index': e.time_index,
                'secondary_time_index': e.secondary_time_index,
                'encoding': e.encoding,
                'variables': {
                    v.id: v.create_metadata_dict()
                    for v in e.variables
                },
                'has_last_time_index': e.last_time_index is not None
            } for eid, e in self.entity_dict.items()},
        }

    @classmethod
    def from_metadata(cls, metadata, data_root=None):
        es = EntitySet(metadata['id'])
        set_last_time_indexes = False
        for eid, entity in metadata['entity_dict'].items():
            df, variable_types = cls._load_dummy_entity_data_and_variable_types(entity)
            if data_root is not None:
                df = load_entity_data(entity, data_root)
            es.entity_from_dataframe(eid,
                                     df,
                                     index=entity['index'],
                                     time_index=entity['time_index'],
                                     secondary_time_index=entity['secondary_time_index'],
                                     encoding=entity['encoding'],
                                     variable_types=variable_types)
            if entity['has_last_time_index']:
                set_last_time_indexes = True
            for vid, v in entity['variables'].items():
                if v['interesting_values'] and len(v['interesting_values']):
                    es[eid][vid].interesting_values = v['interesting_values']
        for rel in metadata['relationships']:
            es.add_relationship(Relationship(
                es[rel['parent_entity']][rel['parent_variable']],
                es[rel['child_entity']][rel['child_variable']],
            ))
        if set_last_time_indexes:
            es.add_last_time_indexes()
        return es

    ###########################################################################
    #   Public getter/setter methods  #########################################
    ###########################################################################

    def __repr__(self):
        repr_out = u"Entityset: {}\n".format(self.id)
        repr_out += u"  Entities:"
        for e in self.entities:
            if e.df.shape:
                repr_out += u"\n    {} [Rows: {}, Columns: {}]".format(
                    e.id, e.df.shape[0], e.df.shape[1])
            else:
                repr_out += u"\n    {} [Rows: None, Columns: None]".format(
                    e.id)
        repr_out += "\n  Relationships:"

        if len(self.relationships) == 0:
            repr_out += u"\n    No relationships"

        for r in self.relationships:
            repr_out += u"\n    %s.%s -> %s.%s" % \
                (r._child_entity_id, r._child_variable_id,
                 r._parent_entity_id, r._parent_variable_id)

        # encode for python 2
        if type(repr_out) != str:
            repr_out = repr_out.encode("utf-8")

        return repr_out

    def add_relationships(self, relationships):
        """Add multiple new relationships to a entityset

        Args:
            relationships (list[Relationship]) : List of new
                relationships.
        """
        return [self.add_relationship(r) for r in relationships][-1]

    def add_relationship(self, relationship):
        """Add a new relationship between entities in the entityset

        Args:
            relationship (Relationship) : Instance of new
                relationship to be added.
        """
        if relationship in self.relationships:
            logger.warning(
                "Not adding duplicate relationship: %s", relationship)
            return self

        # _operations?

        # this is a new pair of entities
        child_e = relationship.child_entity
        child_v = relationship.child_variable.id
        parent_e = relationship.parent_entity
        parent_v = relationship.parent_variable.id
        if not isinstance(child_e[child_v], vtypes.Discrete):
            child_e.convert_variable_type(variable_id=child_v,
                                          new_type=vtypes.Id,
                                          convert_data=False)

        if not isinstance(parent_e[parent_v], vtypes.Discrete):
            parent_e.convert_variable_type(variable_id=parent_v,
                                           new_type=vtypes.Index,
                                           convert_data=False)
        # Empty dataframes (as a result of accessing Entity.metadata)
        # default to object dtypes for discrete variables, but
        # indexes/ids default to ints. In this case, we convert
        # the empty column's type to int
        if (child_e.df.empty and child_e.df[child_v].dtype == object and
                is_numeric_dtype(parent_e.df[parent_v])):
            child_e.df[child_v] = pd.Series(name=child_v, dtype=np.int64)

        parent_dtype = parent_e.df[parent_v].dtype
        child_dtype = child_e.df[child_v].dtype
        msg = u"Unable to add relationship because {} in {} is Pandas dtype {}"\
            u" and {} in {} is Pandas dtype {}."
        if not is_dtype_equal(parent_dtype, child_dtype):
            raise ValueError(msg.format(parent_v, parent_e.id, parent_dtype,
                                        child_v, child_e.id, child_dtype))

        self.relationships.append(relationship)
        self.reset_metadata()
        return self

    def get_pandas_data_slice(self, filter_entity_ids, index_eid,
                              instances, entity_columns=None,
                              time_last=None, training_window=None,
                              verbose=False):
        """
        Get the slice of data related to the supplied instances of the index
        entity.
        """
        eframes_by_filter = {}

        if verbose:
            iterator = make_tqdm_iterator(iterable=filter_entity_ids,
                                          desc="Gathering relevant data",
                                          unit="entity")
        else:
            iterator = filter_entity_ids
        # gather frames for each child, for each parent
        for filter_eid in iterator:
            # get the instances of the top-level entity linked by our instances
            toplevel_slice = self.related_instances(start_entity_id=index_eid,
                                                    final_entity_id=filter_eid,
                                                    instance_ids=instances,
                                                    time_last=time_last,
                                                    training_window=training_window)

            eframes = {filter_eid: toplevel_slice}

            # Do a bredth-first search of the relationship tree rooted at this
            # entity, filling out eframes for each entity we hit on the way.
            r_queue = self.get_backward_relationships(filter_eid)
            while r_queue:
                r = r_queue.pop(0)
                child_eid = r.child_variable.entity.id
                child_columns = None
                if entity_columns is not None and child_eid not in entity_columns:
                    # entity_columns specifies which columns to extract
                    # if it skips a relationship (specifies child and grandparent columns)
                    # we need to at least add the ids of the intermediate entity
                    child_columns = [v.id for v in self[child_eid].variables
                                     if isinstance(v, (vtypes.Index, vtypes.Id,
                                                       vtypes.TimeIndex))]
                elif entity_columns is not None:
                    child_columns = entity_columns[child_eid]

                parent_eid = r.parent_variable.entity.id

                # If we've already seen this child, this is a diamond graph and
                # we don't know what to do
                if child_eid in eframes:
                    raise RuntimeError('Diamond graph detected!')

                # Add this child's children to the queue
                r_queue += self.get_backward_relationships(child_eid)

                # Query the child of the current backwards relationship for the
                # instances we want
                instance_vals = eframes[parent_eid][r.parent_variable.id]
                eframes[child_eid] =\
                    self.entity_dict[child_eid].query_by_values(
                        instance_vals,
                        variable_id=r.child_variable.id,
                        columns=child_columns,
                        time_last=time_last,
                        training_window=training_window)

                # add link variables to this dataframe in order to link it to its
                # (grand)parents
                self._add_multigenerational_link_vars(frames=eframes,
                                                      start_entity_id=filter_eid,
                                                      end_entity_id=child_eid)

            eframes_by_filter[filter_eid] = eframes

        # If there are no instances of *this* entity in the index, return None
        if eframes_by_filter[index_eid][index_eid].shape[0] == 0:
            return None

        return eframes_by_filter

    ###########################################################################
    #   Relationship access/helper methods  ###################################
    ###########################################################################

    def find_path(self, start_entity_id, goal_entity_id,
                  include_num_forward=False):
        """Find a path in the entityset represented as a DAG
           between start_entity and goal_entity

        Args:
            start_entity_id (str) : Id of entity to start the search from.
            goal_entity_id  (str) : Id of entity to find forward path to.
            include_num_forward (bool) : If True, return number of forward
                relationships in path if the path ends on a forward
                relationship, otherwise return 0.

        Returns:
            List of relationships that go from start entity to goal
                entity. None is returned if no path exists.
            If include_forward_distance is True,
                returns a tuple of (relationship_list, forward_distance).

        See Also:
            :func:`BaseEntitySet.find_forward_path`
            :func:`BaseEntitySet.find_backward_path`
        """
        if start_entity_id == goal_entity_id:
            if include_num_forward:
                return [], 0
            else:
                return []

        # BFS so we get shortest path
        start_node = BFSNode(start_entity_id, None, None)
        queue = [start_node]
        nodes = {}

        while len(queue) > 0:
            current_node = queue.pop(0)
            if current_node.entity_id == goal_entity_id:
                path, num_forward = current_node.build_path()
                if include_num_forward:
                    return path, num_forward
                else:
                    return path

            for r in self.get_forward_relationships(current_node.entity_id):
                if r.parent_entity.id not in nodes:
                    parent_node = BFSNode(r.parent_entity.id, current_node, r)
                    nodes[r.parent_entity.id] = parent_node
                    queue.append(parent_node)

            for r in self.get_backward_relationships(current_node.entity_id):
                if r.child_entity.id not in nodes:
                    child_node = BFSNode(r.child_entity.id, current_node, r)
                    nodes[r.child_entity.id] = child_node
                    queue.append(child_node)

        raise ValueError(("No path from {} to {}! Check that all entities "
                          .format(start_entity_id, goal_entity_id)),
                         "are connected by relationships")

    def find_forward_path(self, start_entity_id, goal_entity_id):
        """Find a forward path between a start and goal entity

        Args:
            start_entity_id (str) : id of entity to start the search from
            goal_entity_id  (str) : if of entity to find forward path to

        Returns:
            List of relationships that go from start entity to goal
                entity. None is return if no path exists

        See Also:
            :func:`BaseEntitySet.find_backward_path`
            :func:`BaseEntitySet.find_path`
        """

        if start_entity_id == goal_entity_id:
            return []

        for r in self.get_forward_relationships(start_entity_id):
            new_path = self.find_forward_path(
                r.parent_entity.id, goal_entity_id)
            if new_path is not None:
                return [r] + new_path

        return None

    def find_backward_path(self, start_entity_id, goal_entity_id):
        """Find a backward path between a start and goal entity

        Args:
            start_entity_id (str) : Id of entity to start the search from.
            goal_entity_id  (str) : Id of entity to find backward path to.

        See Also:
            :func:`BaseEntitySet.find_forward_path`
            :func:`BaseEntitySet.find_path`

        Returns:
            List of relationship that go from start entity to goal entity. None
            is returned if no path exists.
        """
        forward_path = self.find_forward_path(goal_entity_id, start_entity_id)
        if forward_path is not None:
            return forward_path[::-1]
        return None

    def get_forward_entities(self, entity_id, deep=False):
        """Get entities that are in a forward relationship with entity

        Args:
            entity_id (str) - Id entity of entity to search from.
            deep (bool) - if True, recursively find forward entities.

        Returns:
            Set of entity IDs in a forward relationship with the passed in
            entity.
        """
        parents = [r.parent_entity.id for r in
                   self.get_forward_relationships(entity_id)]

        if deep:
            parents_deep = set([])
            for p in parents:
                parents_deep.add(p)

                # no loops that are typically caused by one to one relationships
                if entity_id in self.get_forward_entities(p):
                    continue

                to_add = self.get_forward_entities(p, deep=True)
                parents_deep = parents_deep.union(to_add)

            parents = parents_deep

        return set(parents)

    def get_backward_entities(self, entity_id, deep=False):
        """Get entities that are in a backward relationship with entity

        Args:
            entity_id (str) - Id entity of entity to search from.
            deep (bool) - If True, recursively find backward entities.

        Returns:
            Set of each :class:`.Entity` in a backward relationship.
        """
        children = [r.child_entity.id for r in
                    self.get_backward_relationships(entity_id)]
        if deep:
            children_deep = set([])
            for p in children:
                children_deep.add(p)
                to_add = self.get_backward_entities(p, deep=True)
                children_deep = children_deep.union(to_add)

            children = children_deep
        return set(children)

    def get_forward_relationships(self, entity_id):
        """Get relationships where entity "entity_id" is the child

        Args:
            entity_id (str): Id of entity to get relationships for.

        Returns:
            list[:class:`.Relationship`]: List of forward relationships.
        """
        return [r for r in self.relationships if r.child_entity.id == entity_id]

    def get_backward_relationships(self, entity_id):
        """
        get relationships where entity "entity_id" is the parent.

        Args:
            entity_id (str): Id of entity to get relationships for.

        Returns:
            list[:class:`.Relationship`]: list of backward relationships
        """
        return [r for r in self.relationships if r.parent_entity.id == entity_id]

    def get_relationship(self, eid_1, eid_2):
        """Get relationship, if any, between eid_1 and eid_2

        Args:
            eid_1 (str): Id of first entity to get relationships for.
            eid_2 (str): Id of second entity to get relationships for.

        Returns:
            :class:`.Relationship`: Relationship or None
        """
        for r in self.relationships:
            if r.child_entity.id == eid_1 and r.parent_entity.id == eid_2 or \
                    r.parent_entity.id == eid_1 and r.child_entity.id == eid_2:
                return r
        return None

    def _is_backward_relationship(self, rel, prev_ent):
        if prev_ent == rel.parent_entity.id:
            return True
        return False

    def path_relationships(self, path, start_entity_id):
        """
        Generate a list of the strings "forward" or "backward" corresponding to
        the direction of the relationship at each point in `path`.
        """
        prev_entity = start_entity_id
        rels = []
        for r in path:
            if self._is_backward_relationship(r, prev_entity):
                rels.append('backward')
                prev_entity = r.child_variable.entity.id
            else:
                rels.append('forward')
                prev_entity = r.parent_variable.entity.id
        return rels

    ###########################################################################
    #  Entity creation methods  ##############################################
    ###########################################################################

    def entity_from_dataframe(self,
                              entity_id,
                              dataframe,
                              index=None,
                              variable_types=None,
                              make_index=False,
                              time_index=None,
                              secondary_time_index=None,
                              encoding=None,
                              already_sorted=False):
        """
        Load the data for a specified entity from a Pandas DataFrame.

        Args:
            entity_id (str) : Unique id to associate with this entity.

            dataframe (pandas.DataFrame) : Dataframe containing the data.

            index (str, optional): Name of the variable used to index the entity.
                If None, take the first column.

            variable_types (dict[str -> Variable], optional):
                Keys are of variable ids and values are variable types. Used to to
                initialize an entity's store.

            make_index (bool, optional) : If True, assume index does not
                exist as a column in dataframe, and create a new column of that name
                using integers. Otherwise, assume index exists.

            time_index (str, optional): Name of the variable containing
                time data. Type must be in :class:`variables.DateTime` or be
                able to be cast to datetime (e.g. str, float, or numeric.)

            secondary_time_index (dict[str -> Variable]): Name of variable
                containing time data to use a second time index for the entity.

            encoding (str, optional): If None, will use 'ascii'. Another option is
                'utf-8', or any encoding supported by pandas.

            already_sorted (bool, optional) : If True, assumes that input dataframe
                is already sorted by time. Defaults to False.

        Notes:

            Will infer variable types from Pandas dtype

        Example:
            .. ipython:: python

                import featuretools as ft
                import pandas as pd
                transactions_df = pd.DataFrame({"id": [1, 2, 3, 4, 5, 6],
                                                "session_id": [1, 2, 1, 3, 4, 5],
                                                "amount": [100.40, 20.63, 33.32, 13.12, 67.22, 1.00],
                                                "transaction_time": pd.date_range(start="10:00", periods=6, freq="10s"),
                                                "fraud": [True, False, True, False, True, True]})
                es = ft.EntitySet("example")
                es.entity_from_dataframe(entity_id="transactions",
                                         index="id",
                                         time_index="transaction_time",
                                         dataframe=transactions_df)

                es["transactions"]
                es["transactions"].df

        """

        return self._import_from_dataframe(entity_id, dataframe.copy(), index=index,
                                           make_index=make_index,
                                           time_index=time_index,
                                           secondary_time_index=secondary_time_index,
                                           variable_types=variable_types,
                                           encoding=encoding,
                                           already_sorted=already_sorted)

    def normalize_entity(self, base_entity_id, new_entity_id, index,
                         additional_variables=None, copy_variables=None,
                         make_time_index=None,
                         make_secondary_time_index=None,
                         new_entity_time_index=None,
                         new_entity_secondary_time_index=None,
                         time_index_reduce='first'):
        """Create a new entity and relationship from unique values of an existing variable.

        Args:
            base_entity_id (str) : Entity id from which to split.

            new_entity_id (str): Id of the new entity.

            index (str): Variable in old entity
                that will become index of new entity. Relationship
                will be created across this variable.

            additional_variables (list[str]):
                List of variable ids to remove from
                base_entity and move to new entity.

            copy_variables (list[str]): List of
                variable ids to copy from old entity
                and move to new entity.

            make_time_index (bool or str, optional): Create time index for new entity based
                on time index in base_entity, optionally specifying which variable in base_entity
                to use for time_index. If specified as True without a specific variable,
                uses the primary time index. Defaults to True if base entity has a time index.

            make_secondary_time_index (dict[str -> list[str]], optional): Create a secondary time index
                from key. Values of dictionary
                are the variables to associate with the secondary time index. Only one
                secondary time index is allowed. If None, only associate the time index.

            new_entity_time_index (str, optional): Rename new entity time index.

            new_entity_secondary_time_index (str, optional): Rename new entity secondary time index.

            time_index_reduce (str): If making a time_index, choose either
                the 'first' time or the 'last' time from the associated children instances.
                If creating a secondary time index, then the primary time index always reduces
                using 'first', and secondary using 'last'.

        """
        base_entity = self.entity_dict[base_entity_id]
        additional_variables = additional_variables or []
        copy_variables = copy_variables or []

        if not isinstance(additional_variables, list):
            raise TypeError("'additional_variables' must be a list, but received type {}"
                            .format(type(additional_variables)))

        if not isinstance(copy_variables, list):
            raise TypeError("'copy_variables' must be a list, but received type {}"
                            .format(type(copy_variables)))

        for v in additional_variables + copy_variables:
            if v == index:
                raise ValueError("Not copying {} as both index and variable".format(v))
                break
        new_index = index

        transfer_types = {}
        transfer_types[new_index] = type(base_entity[index])
        for v in additional_variables + copy_variables:
            transfer_types[v] = type(base_entity[v])

        # create and add new entity
        new_entity_df = self[base_entity_id].df.copy()

        if make_time_index is None and base_entity.time_index is not None:
            make_time_index = True

        if isinstance(make_time_index, str):
            base_time_index = make_time_index
            new_entity_time_index = base_entity[make_time_index].id
        elif make_time_index:
            base_time_index = base_entity.time_index
            if new_entity_time_index is None:
                new_entity_time_index = "%s_%s_time" % (time_index_reduce, base_entity.id)

            assert base_entity.time_index is not None, \
                "Base entity doesn't have time_index defined"

            if base_time_index not in [v for v in additional_variables]:
                copy_variables.append(base_time_index)

            transfer_types[new_entity_time_index] = type(base_entity[base_entity.time_index])

            new_entity_df.sort_values([base_time_index, base_entity.index],
                                      kind="mergesort",
                                      inplace=True)
        else:
            new_entity_time_index = None

        selected_variables = [index] +\
            [v for v in additional_variables] +\
            [v for v in copy_variables]

        new_entity_df2 = new_entity_df. \
            drop_duplicates(index, keep=time_index_reduce)[selected_variables]

        if make_time_index:
            new_entity_df2.rename(columns={base_time_index: new_entity_time_index}, inplace=True)
        if make_secondary_time_index:
            time_index_reduce = 'first'

            assert len(make_secondary_time_index) == 1, "Can only provide 1 secondary time index"
            secondary_time_index = list(make_secondary_time_index.keys())[0]

            secondary_variables = [index, secondary_time_index] + list(make_secondary_time_index.values())[0]
            secondary_df = new_entity_df. \
                drop_duplicates(index, keep='last')[secondary_variables]
            if new_entity_secondary_time_index:
                secondary_df.rename(columns={secondary_time_index: new_entity_secondary_time_index},
                                    inplace=True)
                secondary_time_index = new_entity_secondary_time_index
            else:
                new_entity_secondary_time_index = secondary_time_index
            secondary_df.set_index(index, inplace=True)
            new_entity_df = new_entity_df2.join(secondary_df, on=index)
        else:
            new_entity_df = new_entity_df2

        base_entity_index = index

        transfer_types[index] = vtypes.Categorical
        if make_secondary_time_index:
            old_ti_name = list(make_secondary_time_index.keys())[0]
            ti_cols = list(make_secondary_time_index.values())[0]
            ti_cols = [c if c != old_ti_name else secondary_time_index for c in ti_cols]
            make_secondary_time_index = {secondary_time_index: ti_cols}

        self._import_from_dataframe(new_entity_id, new_entity_df,
                                    index,
                                    time_index=new_entity_time_index,
                                    secondary_time_index=make_secondary_time_index,
                                    last_time_index=None,
                                    variable_types=transfer_types,
                                    encoding=base_entity.encoding)

        for v in additional_variables:
            self.entity_dict[base_entity_id].delete_variable(v)

        new_entity = self.entity_dict[new_entity_id]
        base_entity.convert_variable_type(base_entity_index, vtypes.Id, convert_data=False)
        self.add_relationship(Relationship(new_entity[index], base_entity[base_entity_index]))
        self.reset_metadata()
        return self

    ###########################################################################
    #  Data wrangling methods  ###############################################
    ###########################################################################

    def concat(self, other, inplace=False):
        '''Combine entityset with another to create a new entityset with the
        combined data of both entitysets.
        '''
        assert_string = "Entitysets must have the same entities, relationships"\
            ", and variable_ids"
        assert (self.__eq__(other) and
                self.relationships == other.relationships), assert_string

        for entity in self.entities:
            assert entity.id in other.entity_dict, assert_string
            assert (len(self[entity.id].variables) ==
                    len(other[entity.id].variables)), assert_string
            other_variable_ids = [o_variable.id for o_variable in
                                  other[entity.id].variables]
            assert (all([variable.id in other_variable_ids
                         for variable in self[entity.id].variables])), assert_string

        if inplace:
            combined_es = self
        else:
            combined_es = copy.deepcopy(self)

        has_last_time_index = []
        for entity in self.entities:
            self_df = entity.df
            other_df = other[entity.id].df
            combined_df = pd.concat([self_df, other_df])
            if entity.created_index == entity.index:
                columns = [col for col in combined_df.columns if
                           col != entity.index or col != entity.time_index]
            else:
                columns = [entity.index]
            combined_df.drop_duplicates(columns, inplace=True)

            if entity.time_index:
                combined_df.sort_values([entity.time_index, entity.index], inplace=True)
            else:
                combined_df.sort_index(inplace=True)
            if (entity.last_time_index is not None or
                    other[entity.id].last_time_index is not None):
                has_last_time_index.append(entity.id)
            combined_es[entity.id].update_data(df=combined_df,
                                               recalculate_last_time_indexes=False)

        combined_es.add_last_time_indexes(updated_entities=has_last_time_index)
        self.reset_metadata()
        return combined_es

    ###########################################################################
    #  Indexing methods  ###############################################
    ###########################################################################
    def add_last_time_indexes(self, updated_entities=None):
        """
        Calculates the last time index values for each entity (the last time
        an instance or children of that instance were observed).  Used when
        calculating features using training windows
        Args:
          * updated_entities (list[str]): List of entity ids to update last_time_index for
                (will update all parents of those entities as well)
        """
        # Generate graph of entities to find leaf entities
        children = defaultdict(list)  # parent --> child mapping
        child_vars = defaultdict(dict)
        for r in self.relationships:
            children[r.parent_entity.id].append(r.child_entity)
            child_vars[r.parent_entity.id][r.child_entity.id] = r.child_variable

        updated_entities = updated_entities or []
        if updated_entities:
            # find parents of updated_entities
            parent_queue = updated_entities[:]
            parents = set()
            while len(parent_queue):
                e = parent_queue.pop(0)
                if e in parents:
                    continue
                parents.add(e)
                for parent_id in self[e].parents:
                    parent_queue.append(parent_id)
            queue = [self[p] for p in parents]
            to_explore = parents
        else:
            to_explore = set([e.id for e in self.entities[:]])
            queue = self.entities[:]

        explored = set()

        for e in queue:
            e.last_time_index = None

        # We will explore children of entities on the queue,
        # which may not be in the to_explore set. Therefore,
        # we check whether all elements of to_explore are in
        # explored, rather than just comparing length
        while not to_explore.issubset(explored):
            entity = queue.pop(0)

            if entity.last_time_index is None:
                if entity.time_index is not None:
                    lti = entity.df[entity.time_index].copy()
                else:
                    lti = entity.df[entity.index].copy()
                    lti[:] = None
                entity.last_time_index = lti

            if entity.id in children:
                child_entities = children[entity.id]

                # if all children not explored, skip for now
                if not set([e.id for e in child_entities]).issubset(explored):
                    # Now there is a possibility that a child entity
                    # was not explicitly provided in updated_entities,
                    # and never made it onto the queue. If updated_entities
                    # is None then we just load all entities onto the queue
                    # so we didn't need this logic
                    for e in child_entities:
                        if e.id not in explored and e.id not in [q.id for q in queue]:
                            queue.append(e)
                    queue.append(entity)
                    continue

                # updated last time from all children
                for child_e in child_entities:
                    if child_e.last_time_index is None:
                        continue
                    link_var = child_vars[entity.id][child_e.id].id
                    lti_df = pd.DataFrame({'last_time': child_e.last_time_index,
                                           entity.index: child_e.df[link_var]})
                    # sort by time and keep only the most recent
                    lti_df.sort_values(['last_time', entity.index],
                                       kind="mergesort", inplace=True)
                    lti_df.drop_duplicates(entity.index,
                                           keep='last',
                                           inplace=True)

                    lti_df.set_index(entity.index, inplace=True)
                    lti_df = lti_df.reindex(entity.last_time_index.index)
                    lti_df['last_time_old'] = entity.last_time_index
                    lti_df = lti_df.apply(lambda x: x.dropna().max(), axis=1)
                    entity.last_time_index = lti_df
                    entity.last_time_index.name = 'last_time'

            explored.add(entity.id)
        self.reset_metadata()

    ###########################################################################
    #  Other ###############################################
    ###########################################################################

    def add_interesting_values(self, max_values=5, verbose=False):
        """Find interesting values for categorical variables, to be used to generate "where" clauses

        Args:
            max_values (int) : Maximum number of values per variable to add.
            verbose (bool) : If True, print summary of interesting values found.

        Returns:
            None

        """
        for entity in self.entities:
            entity.add_interesting_values(max_values=max_values, verbose=verbose)
        self.reset_metadata()

    def related_instances(self, start_entity_id, final_entity_id,
                          instance_ids=None, time_last=None,
                          training_window=None):
        """
        Filter out all but relevant information from dataframes along path
        from start_entity_id to final_entity_id,
        exclude data if it does not lie within  and time_last

        Args:
            start_entity_id (str) : Id of start entity.
            final_entity_id (str) : Id of final entity.
            instance_ids (list[str]) : List of start entity instance ids from
                which to find related instances in final entity.
            time_last (pd.TimeStamp) :  Latest allowed time.

        Returns:
            pd.DataFrame : Dataframe of related instances on the final_entity_id
        """
        # Load the filtered dataframe for the first entity
        training_window_is_dict = isinstance(training_window, dict)
        window = training_window
        start_estore = self.entity_dict[start_entity_id]
        # This check might be brittle
        if instance_ids is not None and not hasattr(instance_ids, '__iter__'):
            instance_ids = [instance_ids]

        if training_window_is_dict:
            window = training_window.get(start_estore.id)
        df = start_estore.query_by_values(instance_vals=instance_ids,
                                          time_last=time_last,
                                          training_window=window)
        # if we're querying on a path that's not actually a path, just return
        # the relevant slice of the entityset
        if start_entity_id == final_entity_id:
            return df

        # get relationship path from start to end entity
        path = self.find_path(start_entity_id, final_entity_id)
        if path is None or len(path) == 0:
            return pd.DataFrame()

        prev_entity_id = start_entity_id

        # Walk down the path of entities and take related instances at each step
        for i, r in enumerate(path):
            new_entity_id = r.get_other_entity(prev_entity_id)
            rvar_old = r.get_entity_variable(prev_entity_id)
            rvar_new = r.get_entity_variable(new_entity_id)
            all_ids = df[rvar_old]

            # filter the next entity by the values found in the previous
            # entity's relationship column
            entity_store = self.entity_dict[new_entity_id]
            if training_window_is_dict:
                window = training_window.get(entity_store.id)
            df = entity_store.query_by_values(all_ids,
                                              variable_id=rvar_new,
                                              time_last=time_last,
                                              training_window=window)

            prev_entity_id = new_entity_id

        return df

    def gen_relationship_var(self, child_eid, parent_eid):
        path = self.find_path(parent_eid, child_eid)
        r = path.pop(0)
        child_link_name = r.child_variable.id
        for r in path:
            parent_entity = r.parent_entity
            parent_link_name = child_link_name
            child_link_name = '%s.%s' % (parent_entity.id,
                                         parent_link_name)
        return child_link_name

    ###########################################################################
    #  Private methods  ######################################################
    ###########################################################################

    def _import_from_dataframe(self,
                               entity_id,
                               dataframe,
                               index=None,
                               variable_types=None,
                               make_index=False,
                               time_index=None,
                               secondary_time_index=None,
                               last_time_index=None,
                               parse_date_cols=None,
                               encoding=None,
                               already_sorted=False):
        """
        Load the data for a specified entity from a pandas dataframe.

        Args:
            entity_id (str) : Unique id to associate with this entity.
            dataframe (pd.DataFrame) : Pandas dataframe containing the data.
            index (str, optional): Name of the variable used to index the entity.
                If None, take the first column.
            variable_types (dict[str -> dict[str -> type]]) : Optional mapping of
                entity_id to variable_types dict with which to initialize an
                entity's store.
            make_index (bool, optional) : If True, assume index does not exist as a column in
                dataframe, and create a new column of that name using integers the (0, len(dataframe)).
                Otherwise, assume index exists in dataframe.
                An entity's variable_types dict maps string variable ids to types (:class:`.Variable`).
            time_index (str, optional) : Name of column to use as a time index for this entity. Must be
                a Datetime or Numeric dtype.
            secondary_time_index (str, optional): Name of variable containing
                time data to use a second time index for the entity.
            encoding (str, optional) : If None, will use 'ascii'. Another option is 'utf-8',
                or any encoding supported by pandas. Passed into underlying pandas.to_csv() calls,
                so see Pandas documentation for more information.
            already_sorted (bool, optional) : If True, assumes that input dataframe is already sorted by time.
                Defaults to False.
        """
        variable_types = variable_types or {}

        # DFS TODO: confirm we want this if else block
        if index is None:
            assert not make_index, "Must specify an index name if make_index is True"
            logger.warning(("Using first column as index. ",
                            "To change this, specify the index parameter"))
        else:
            if index not in variable_types:
                variable_types[index] = vtypes.Index

        created_index = None
        if make_index or index not in dataframe.columns:
            if not make_index:
                logger.warning("index %s not found in dataframe, creating new integer column",
                               index)
            if index in dataframe.columns:
                raise RuntimeError("Cannot make index: index variable already present")
            dataframe.insert(0, index, range(0, len(dataframe)))
            created_index = index
        elif index is None:
            index = dataframe.columns[0]

        elif time_index is not None and time_index not in dataframe.columns:
            raise LookupError('Time index not found in dataframe')
        if parse_date_cols is not None:
            for c in parse_date_cols:
                variable_types[c] = vtypes.Datetime

        current_relationships = [r for r in self.relationships
                                 if r.parent_entity.id == entity_id or
                                 r.child_entity.id == entity_id]

        for c in dataframe.columns:
            if dataframe[c].dtype.name.find('category') > -1:
                if c not in variable_types:
                    variable_types[c] = vtypes.Categorical

        entity = Entity(entity_id,
                        dataframe,
                        self,
                        variable_types=variable_types,
                        index=index,
                        time_index=time_index,
                        secondary_time_index=secondary_time_index,
                        last_time_index=last_time_index,
                        encoding=encoding,
                        relationships=current_relationships,
                        already_sorted=already_sorted,
                        created_index=created_index)
        self.entity_dict[entity.id] = entity
        self.reset_metadata()
        return self

    def _add_multigenerational_link_vars(self, frames, start_entity_id,
                                         end_entity_id=None, path=None):
        """
        Add multi-generational link variables to entity dataframes in order to
        keep track of deep relationships.

        For example: if entity 'grandparent' has_many of entity 'parent' which
        has_many of entity 'child', and parent is related to grandparent by
        variable 'grandparent_id', add a column to child called
        'parent.grandparent_id' so that child instances can be grouped by
        grandparent_id as well.

        This function adds link variables to all relationships along the
        provided path.
        """

        # caller can pass either a path or a start/end entity pair

        assert start_entity_id is not None
        if path is None:
            assert end_entity_id is not None
            path = self.find_path(start_entity_id, end_entity_id)

        directions = self.path_relationships(path, start_entity_id)
        relationship_directions = list(zip(directions, path))
        groups = itertools.groupby(relationship_directions, key=lambda k: k[0])

        # each group is a contiguous series of backward relationships on `path`
        for key, group in groups:
            if key != 'backward':
                continue

            # extract the path again
            chain = [g[1] for g in group]

            # generate a list of all sub-paths which have at least 2
            # relationships
            rel_chains = [chain[i:] for i in range(len(chain) - 1)]

            # loop over all subpaths
            for chain in rel_chains:
                # pop off the first relationship: this one already has a
                # direct variable link, but we'll need to remember its link
                # variable name for later.
                r = chain.pop(0)
                child_link_name = r.child_variable.id

                # step through each deep relationship of the subpath
                for r in chain:
                    parent_entity = r.parent_entity
                    child_entity = r.child_entity
                    parent_df = frames[parent_entity.id]
                    child_df = frames[child_entity.id]

                    # generate the link variable name
                    parent_link_name = child_link_name
                    child_link_name = '%s.%s' % (parent_entity.id,
                                                 parent_link_name)
                    if child_link_name in child_df.columns:
                        continue

                    # print 'adding link var %s to entity %s' % (child_link_name,
                    #                                            child_entity.id)

                    # create an intermediate dataframe which shares a column
                    # with the child dataframe and has a column with the
                    # original parent's id.
                    col_map = {r.parent_variable.id: r.child_variable.id,
                               parent_link_name: child_link_name}
                    merge_df = parent_df[list(col_map.keys())].rename(columns=col_map)

                    # merge the dataframe, adding the link variable to the child
                    frames[child_entity.id] = pd.merge(left=merge_df,
                                                       right=child_df,
                                                       on=r.child_variable.id)

    @classmethod
    def _load_dummy_entity_data_and_variable_types(cls, metadata):
        variable_types = {}
        defaults = []
        columns = []
        variable_names = {}
        for elt in dir(vtypes):
            try:
                cls = getattr(vtypes, elt)
                if issubclass(cls, vtypes.Variable):
                    variable_names[cls._dtype_repr] = cls
            except TypeError:
                pass
        for vid, vmetadata in metadata['variables'].items():
            if vmetadata['dtype_repr']:
                vtype = variable_names.get(vmetadata['dtype_repr'], vtypes.Variable)
                variable_types[vid] = vtype
                defaults.append(vtypes.DEFAULT_DTYPE_VALUES[vtype._default_pandas_dtype])
            else:
                defaults.append(vtypes.DEFAULT_DTYPE_VALUES[object])
            columns.append(vid)
        df = pd.DataFrame({c: [d] for c, d in zip(columns, defaults)}).head(0)
        return df, variable_types


def make_index_variable_name(entity_id):
    return entity_id + "_id"
