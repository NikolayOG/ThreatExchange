# Copyright (c) Meta Platforms, Inc. and affiliates.

"""
Helpers for implementing SignalExchangeAPIs or state.

They demonstrate how the methods in SignalExchangeAPI can be used to 
reproduce databases of signals, as well as simple recipes for stage.
"""

from dataclasses import dataclass, field
import logging
import typing as t
from threatexchange.exchanges.signal_exchange_api import TSignalExchangeAPICls

from threatexchange.signal_type.signal_base import SignalType
from threatexchange.exchanges import fetch_state
from threatexchange.exchanges.collab_config import CollaborationConfigBase
from threatexchange.exchanges.storage import DBMStorage

K = t.TypeVar("K")
V = t.TypeVar("V")


@dataclass
class SimpleFetchedSignalMetadata(fetch_state.FetchedSignalMetadata):
    """
    Simple dataclass for fetched data.

    Merge by addition rather than replacement.
    """

    opinions: t.List[fetch_state.SignalOpinion] = field(default_factory=list)

    def get_as_opinions(self) -> t.Sequence[fetch_state.SignalOpinion]:
        return self.opinions


@dataclass
class _StateTracker:
    api_cls: TSignalExchangeAPICls
    _delta: t.Optional[fetch_state.FetchDeltaTyped]
    dirty: bool = False

    @property
    def empty(self) -> bool:
        return self._delta is None

    @property
    def delta(self) -> fetch_state.FetchDeltaTyped:
        assert self._delta is not None
        return self._delta

    @delta.setter
    def delta(self, value: fetch_state.FetchDeltaTyped) -> None:
        if self._delta is None:
            old = {}
            self._delta = value
        else:
            old = self._delta.updates
        self.api_cls.naive_fetch_merge(old, value.updates)
        self._delta.updates = old
        self._delta.checkpoint = value.checkpoint
        self.dirty = True

    @property
    def checkpoint(self) -> t.Optional[fetch_state.FetchCheckpointBase]:
        return None if self._delta is None else self._delta.checkpoint


class SimpleFetchedStateStore(fetch_state.FetchedStateStoreBase):
    """
    A FetchedStateStore that does merges in memory and writes by collab
    """

    def __init__(
        self,
        api_cls: TSignalExchangeAPICls,
    ) -> None:
        self.api_cls = api_cls
        self._state: t.Dict[str, _StateTracker] = {}

    def _read_state(
        self,
        collab_name: str,
    ) -> t.Optional[fetch_state.FetchDeltaTyped]:
        raise NotImplementedError

    def _write_state(
        self, collab_name: str, delta: fetch_state.FetchDeltaTyped
    ) -> None:
        raise NotImplementedError

    def get_checkpoint(
        self, collab: CollaborationConfigBase
    ) -> t.Optional[fetch_state.FetchCheckpointBase]:
        return self._get_state(collab).checkpoint

    def _get_state(self, collab: CollaborationConfigBase) -> _StateTracker:
        if collab.name not in self._state:
            logging.debug("Loading state for %s", collab.name)
            delta = self._read_state(collab.name)
            self._state[collab.name] = _StateTracker(self.api_cls, delta)
        return self._state[collab.name]

    def clear(self, collab: CollaborationConfigBase) -> None:
        self._state.pop(collab.name, None)

    def merge(
        self,
        collab: CollaborationConfigBase,
        delta: fetch_state.FetchDeltaTyped,
    ) -> None:
        """
        Merge a FetchDeltaBase into the state.

        At the implementation's discretion, it may call flush() or the
        equivalent work.
        """

        state = self._get_state(collab)
        if len(delta.updates) == 0 and delta.checkpoint in (
            None,
            state.checkpoint,
        ):
            logging.warning("No op update for %s", collab.name)
            return
        state.delta = delta

    def flush(self, collab: CollaborationConfigBase) -> None:
        for collab_name, state in self._state.items():
            if state.dirty:
                assert state.delta is not None
                self._write_state(collab_name, state.delta)
                state.dirty = False

    def get_for_signal_type(
        self, collabs: t.List[CollaborationConfigBase], signal_type: t.Type[SignalType]
    ) -> t.Dict[str, t.Dict[str, fetch_state.FetchedSignalMetadata]]:
        ret = {}
        for collab in collabs:
            state = self._get_state(collab)
            if not state.empty:
                by_signal = state.api_cls.naive_convert_to_signal_type(
                    [signal_type], collab, state.delta.updates
                ).get(signal_type, {})
                if by_signal:
                    ret[collab.name] = by_signal
        return ret

    def exists(self, collab: CollaborationConfigBase) -> bool:
        raise NotImplementedError


class DBMFetchedStateStore(fetch_state.FetchedStateStoreBase):
    def __init__(
        self,
        api_cls: TSignalExchangeAPICls,
    ) -> None:
        self.api_cls = api_cls


    def get_for_signal_type(
        self, collabs: t.List[CollaborationConfigBase], signal_type: t.Type[SignalType]
    ) -> t.Dict[str, t.Dict[str, fetch_state.FetchedSignalMetadata]]:
        """
        Get as a map of CollabConfigBase.name() => {signal: Metadata}

        This is meant for simple storage and indexing solutions, but at
        scale, you likely want to store as IDs rather than the full metadata.

        TODO: This currently implies that you are going to load the entire dataset
        into memory, which once we start getting huge amounts of data, might not make
        sense.
        """

        ret = {}
        for collab in collabs:
            db = DBMStorage().connect(collab.name)
            data = {k: v for k, v in db}
            by_signal = self.api_cls.naive_convert_to_signal_type([signal_type], collab, data).get(signal_type, {})
            if by_signal:
                ret[collab.name] = by_signal
        return ret
    
    def clear(self, collab: CollaborationConfigBase) -> None:
        DBMStorage().connect(collab.name).drop()

    def exists(self, collab: CollaborationConfigBase) -> bool:
        return DBMStorage().connect(collab.name).exists()

    def flush(self, collab: CollaborationConfigBase) -> None:
        """
        Finish writing the results of previous merges to persistant state.

        This should also persist the checkpoint.
        """
        db = DBMStorage().connect(f'{collab.name}_checkpoint')
        # "commit" the current temp _checkpoint as permanent
        db.set('checkpoint', db.get('_checkpoint'))
    
    def merge(self, collab: CollaborationConfigBase, delta: fetch_state.FetchDelta) -> None:
        db_cp = DBMStorage().connect(f'{collab.name}_checkpoint')
        if len(delta.updates) == 0 and delta.checkpoint in (
            None,
            db_cp.get('checkpoint'),
        ):
            logging.warning("No op update for %s", collab.name)
            return

        db = DBMStorage().connect(collab.name)
        for k, v in delta.updates.items():
            db.set(k, v)
        
        # Save _checkpoint value but "commit" it in flush() 
        db_cp.set('_checkpoint', delta.checkpoint)
            
    def get_checkpoint(
        self, collab: CollaborationConfigBase
    ) -> t.Optional[fetch_state.FetchCheckpointBase]:
        """
        Returns the last checkpoint passed to merge() after a flush()
        """
        db = DBMStorage().connect(f'{collab.name}_checkpoint')
        return db.get('checkpoint')