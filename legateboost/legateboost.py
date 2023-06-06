from __future__ import annotations

import warnings
from enum import IntEnum
from typing import Any, Union

import numpy as np
import scipy.sparse as sp
from sklearn.base import BaseEstimator, ClassifierMixin, RegressorMixin
from sklearn.utils.validation import check_is_fitted, check_random_state

import cunumeric as cn
from legate.core import Store, types

from .library import user_context, user_lib
from .objectives import objectives


class LegateBoostOpCode(IntEnum):
    BUILD_TREE = user_lib.cffi.BUILD_TREE
    PREDICT = user_lib.cffi.PREDICT


class TreeStructure:
    """A structure of arrays representing a decision tree.

    A leaf node has value -1 at feature[node_idx]
    """

    leaf_value: cn.ndarray
    feature: cn.ndarray
    split_value: cn.ndarray

    def is_leaf(self, id: int) -> Any:
        return self.feature[id] == -1

    def left_child(self, id: int) -> int:
        return id * 2 + 1

    def right_child(self, id: int) -> int:
        return id * 2 + 2

    def __init__(
        self,
        leaf_value: cn.ndarray,
        feature: cn.ndarray,
        split_value: cn.ndarray,
    ) -> None:
        """Initialise from existing storage."""
        self.leaf_value = leaf_value
        assert np.issubdtype(leaf_value.dtype, np.floating)
        self.feature = feature
        assert np.issubdtype(feature.dtype, np.integer)
        self.split_value = split_value
        assert np.issubdtype(split_value.dtype, np.floating)

    def predict(self, X: cn.ndarray) -> cn.ndarray:
        task = user_context.create_auto_task(
            LegateBoostOpCode.PREDICT,
        )
        task.add_input(_get_legate_store(X))
        task.add_input(_get_legate_store(self.leaf_value))
        task.add_broadcast(_get_legate_store(self.leaf_value))
        task.add_input(_get_legate_store(self.feature))
        task.add_broadcast(_get_legate_store(self.feature))
        task.add_input(_get_legate_store(self.split_value))
        task.add_broadcast(_get_legate_store(self.split_value))

        pred = user_context.create_store(types.float64, X.shape[0])
        task.add_output(pred)
        task.execute()
        return cn.array(pred, copy=False)

    def __str__(self) -> str:
        def recurse_print(id: int, depth: int) -> str:
            if self.is_leaf(id):
                text = "\t" * depth + "{}:leaf={}\n".format(id, self.leaf_value[id])
            else:
                text = "\t" * depth + "{}:[f{}<={}] yes={} no={}\n".format(
                    id,
                    self.feature[id],
                    self.split_value[id],
                    self.left_child(id),
                    self.right_child(id),
                )
                text += recurse_print(self.left_child(id), depth + 1)
                text += recurse_print(self.right_child(id), depth + 1)
            return text

        return recurse_print(0, 0)


def _get_legate_store(input: Any) -> Store:
    """Extracts a Legate store from any object implementing the legete data
    interface.

    Args:
        input (Any): The input object

    Returns:
        Store: The extracted Legate store
    """
    if isinstance(input, Store):
        return input
    data = input.__legate_data_interface__["data"]
    field = next(iter(data))
    array = data[field]
    _, store = array.stores()
    return store


def build_tree_native(
    X: cn.ndarray,
    g: cn.ndarray,
    h: cn.ndarray,
    learning_rate: float,
    max_depth: int,
    random_state: np.random.RandomState,
) -> TreeStructure:

    # choose possible splits
    split_proposals = X[random_state.randint(0, X.shape[0], max_depth)]

    task = user_context.create_auto_task(
        LegateBoostOpCode.BUILD_TREE,
    )
    task.add_input(_get_legate_store(X))
    task.add_broadcast(_get_legate_store(X), axes=0)
    task.add_input(_get_legate_store(g))
    task.add_input(_get_legate_store(h))
    task.add_input(_get_legate_store(split_proposals))
    task.add_broadcast(_get_legate_store(split_proposals))
    task.add_alignment(_get_legate_store(g), _get_legate_store(h))
    task.add_scalar_arg(learning_rate, types.float64)
    task.add_scalar_arg(max_depth, types.int32)
    task.add_scalar_arg(random_state.randint(0, 2**32), types.uint64)

    max_nodes = 2 ** (max_depth + 1)
    leaf_value = user_context.create_store(types.float64, max_nodes)
    feature = user_context.create_store(types.int32, max_nodes)
    split_value = user_context.create_store(types.float64, max_nodes)

    task.add_output(leaf_value)
    task.add_output(feature)
    task.add_output(split_value)
    task.add_cpu_communicator()
    task.execute()

    return TreeStructure(
        cn.array(leaf_value, copy=False),
        cn.array(feature, copy=False),
        cn.array(split_value, copy=False),
    )


def _check_sample_weight(sample_weight: Any, n: int) -> cn.ndarray:
    if sample_weight is None:
        sample_weight = cn.ones(n)
    elif cn.isscalar(sample_weight):
        sample_weight = cn.full(n, sample_weight)
    elif not isinstance(sample_weight, cn.ndarray):
        sample_weight = cn.array(sample_weight)
    if sample_weight.shape != (n,):
        raise ValueError(
            "Incorrect sample weight shape: "
            + str(sample_weight.shape)
            + ", expected: ("
            + str(n)
            + ",)"
        )
    return sample_weight


def check_array(x: Any) -> cn.ndarray:
    if not hasattr(x, "__legate_data_interface__"):
        warnings.warn(
            "Input of type {} does not implement ".format(type(x))
            + "__legate_data_interface__. Performance may be affected."
        )
    if hasattr(x, "__array_interface__"):
        shape = x.__array_interface__["shape"]
        if shape[0] <= 0:
            raise ValueError(
                "Found array with %d sample(s) (shape=%s) while a"
                " minimum of %d is required." % (shape[0], shape, 1)
            )
        if len(shape) >= 2 and 0 in shape:
            raise ValueError(
                "Found array with %d feature(s) (shape=%s) while"
                " a minimum of %d is required." % (shape[1], shape, 1)
            )

    if sp.issparse(x):
        raise ValueError("Sparse matrix not allowed.")

    if not cn.isfinite(x).all():
        raise ValueError("Input contains NaN or inf")

    x = cn.array(x, copy=False)
    return x


def check_X_y(X: Any, y: Any) -> tuple[cn.array, cn.array]:
    X = check_array(X)
    y = check_array(y)

    # TODO(Rory): multi-class support
    y = y.squeeze()

    # allow conversion of y but not X for memory reasons
    assert X.dtype.kind == "f"
    if y.dtype.kind != "f":
        y = y.astype(cn.float64)

    assert len(X.shape) == 2
    assert y.shape[0] == X.shape[0]
    return X, y


class LBBase(BaseEstimator):
    def __init__(
        self,
        n_estimators: int = 100,
        objective: str = "squared_error",
        learning_rate: float = 0.1,
        init: Union[str, None] = "average",
        verbose: int = 0,
        random_state: np.random.RandomState = None,
        max_depth: int = 3,
        version: str = "native",
    ) -> None:
        self.n_estimators = n_estimators
        self.objective = objective
        self.learning_rate = learning_rate
        self.init = init
        self.verbose = verbose
        self.random_state = random_state
        self.max_depth = max_depth
        self.version = version

    def _more_tags(self) -> Any:
        return {
            "_xfail_checks": {
                "check_sample_weights_invariance": (
                    "zero sample_weight is not equivalent to removing samples"
                ),
                "check_sample_weights_not_an_array": (
                    "LegateBoost does not convert inputs."
                ),
                "check_complex_data": (
                    "LegateBoost does not currently support complex data."
                ),
                "check_dtype_object": ("object type data not supported."),
            }
        }

    def fit(
        self, X: cn.ndarray, y: cn.ndarray, sample_weight: cn.ndarray = None
    ) -> "LBRegressor":
        X, y = check_X_y(X, y)
        sample_weight = _check_sample_weight(sample_weight, len(y))
        self.n_features_in_ = X.shape[1]
        self.models_ = []

        objective = objectives[self.objective]()
        self.model_init_ = 0.0
        if self.init == "average":
            # initialise the model to some good average value
            # this is equivalent to a tree with a single leaf and learning rate 1.0
            g, h = objective.gradient(
                y, cn.full_like(y, objective.transform(self.model_init_)), sample_weight
            )
            H = h.sum()
            if H > 0.0:
                self.model_init_ = -g.sum() / H

        # current model prediction
        pred = cn.full(y.shape, self.model_init_)
        self._metric = objective.metric()
        self.train_metric_ = []
        for i in range(self.n_estimators):
            # obtain gradients
            g, h = objective.gradient(y, objective.transform(pred), sample_weight)

            # build new tree
            tree = build_tree_native(
                X,
                g,
                h,
                self.learning_rate,
                self.max_depth,
                check_random_state(self.random_state),
            )
            self.models_.append(tree)

            # update current predictions
            pred += self.models_[-1].predict(X)

            # evaluate our progress
            self.train_metric_.append(
                self._metric.metric(y, objective.transform(pred), sample_weight)
            )
            if self.verbose:
                print(
                    "i: {} {}: {}".format(
                        i, self._metric.name(), self.train_metric_[-1]
                    )
                )
        self.is_fitted_ = True
        return self

    def predict(self, X: cn.ndarray) -> cn.ndarray:
        X = check_array(X)
        check_is_fitted(self, "is_fitted_")
        pred = cn.full(X.shape[0], self.model_init_)
        for m in self.models_:
            pred += m.predict(X)
        return pred

    def dump_trees(self) -> str:
        check_is_fitted(self, "is_fitted_")
        text = "init={}\n".format(self.model_init_)
        for m in self.models_:
            text += str(m)
        return text


class LBRegressor(LBBase, RegressorMixin):
    def __init__(
        self,
        n_estimators: int = 100,
        objective: str = "squared_error",
        learning_rate: float = 0.1,
        init: Union[str, None] = "average",
        verbose: int = 0,
        random_state: np.random.RandomState = None,
        max_depth: int = 3,
        version: str = "native",
    ) -> None:
        super().__init__(
            n_estimators=n_estimators,
            objective=objective,
            learning_rate=learning_rate,
            init=init,
            verbose=verbose,
            random_state=random_state,
            max_depth=max_depth,
            version=version,
        )


class LBClassifier(LBBase, ClassifierMixin):
    def __init__(
        self,
        n_estimators: int = 100,
        objective: str = "log_loss",
        learning_rate: float = 0.1,
        init: Union[str, None] = "average",
        verbose: int = 0,
        random_state: np.random.RandomState = None,
        max_depth: int = 3,
        version: str = "native",
    ) -> None:
        super().__init__(
            n_estimators=n_estimators,
            objective=objective,
            learning_rate=learning_rate,
            init=init,
            verbose=verbose,
            random_state=random_state,
            max_depth=max_depth,
            version=version,
        )

    def predict_proba(self, X: cn.ndarray) -> cn.ndarray:
        objective = objectives[self.objective]()
        return objective.transform(super().predict(X))

    def predict(self, X: cn.ndarray) -> cn.ndarray:
        return self.predict_proba(X) >= 0.5