# -*- coding: utf-8 -*-

"""Export and import of generic Python models.

This module defines generic filesystem format for Python models and provides utilities
for saving and loading to and from this format. The format is self contained in the sense
that it includes all necessary information for anyone to load it and use it. Dependencies
are either stored directly with the model or referenced via a Conda environment.

The convention for pyfunc models is to have a ``predict`` method or function with the following
signature:

    predict(data: pandas.DataFrame) -> numpy.ndarray | pandas.Series | pandas.DataFrame

This convention is relied on by other MLflow components.

Pyfunc model format is defined as a directory structure containing all required data, code, and
configuration:

.. code::

    ./dst-path/
            ./MLmodel: configuration
            <code>: code packaged with the model (specified in the MLmodel file)
            <data>: data packaged with the model (specified in the MLmodel file)
            <env>: Conda environment definition (specified in the MLmodel file)

It must contain MLmodel file in its root with "python_function" format with the following
parameters:

- loader_module [required]:
         Python module that can load the model. Expected as module identifier
         e.g. ``mlflow.sklearn``, it will be imported via ``importlib.import_module``.
         The imported module must contain function with the following signature::

          load_pyfunc(path: string) -> <pyfunc model>

         The path argument is specified by the ``data`` parameter and may refer to a file or
         directory.

- code [optional]:
        Relative path to a directory containing the code packaged with this model.
        All files and directories inside this directory are added to the Python path
        prior to importing the model loader.

- data [optional]:
         Relative path to a file or directory containing model data.
         The path is passed to the model loader.

- env [optional]:
         Relative path to an exported Conda environment. If present this environment
         should be activated prior to running the model.

Example:

.. code:: shell

  >tree example/sklearn_iris/mlruns/run1/outputs/linear-lr
  ├── MLmodel
  ├── code
  │   ├── sklearn_iris.py
  │  
  ├── data
  │   └── model.pkl
  └── mlflow_env.yml

  >cat example/sklearn_iris/mlruns/run1/outputs/linear-lr/MLmodel
  python_function:
    code: code
    data: data/model.pkl
    loader_module: mlflow.sklearn
    env: mlflow_env.yml
    main: sklearn_iris
"""

import importlib
import os
import shutil
import sys
import pandas

from mlflow.tracking.fluent import active_run, log_artifacts
from mlflow import tracking
from mlflow.models import Model
from mlflow.utils import PYTHON_VERSION, get_major_minor_py_version
from mlflow.utils.file_utils import TempDir
from mlflow.utils.logging_utils import eprint

FLAVOR_NAME = "python_function"
MAIN = "loader_module"
CODE = "code"
DATA = "data"
ENV = "env"
PY_VERSION = "python_version"


def add_to_model(model, loader_module, data=None, code=None, env=None):
    """
    Add a pyfunc spec to the model configuration.

    Defines pyfunc configuration schema. Caller can use this to create a valid pyfunc model flavor
    out of an existing directory structure. For example, other model flavors can use this to specify
    how to use their output as a pyfunc.

    NOTE: all paths are relative to the exported model root directory.

    :param loader_module: The module to be used to load the model.
    :param model: Existing model.
    :param data: Path to the model data.
    :param code: Path to the code dependencies.
    :param env: Conda environment.
    :return: Updated model configuration.
    """
    parms = {MAIN: loader_module}
    parms[PY_VERSION] = PYTHON_VERSION
    if code:
        parms[CODE] = code
    if data:
        parms[DATA] = data
    if env:
        parms[ENV] = env
    return model.add_flavor(FLAVOR_NAME, **parms)


def _load_model_conf(path, run_id=None):
    """Load a model configuration stored in Python function format."""
    if run_id:
        path = tracking.utils._get_model_log_dir(path, run_id)
    conf_path = os.path.join(path, "MLmodel")
    model = Model.load(conf_path)
    if FLAVOR_NAME not in model.flavors:
        raise Exception("Format '{format}' not found not in {path}.".format(format=FLAVOR_NAME,
                                                                            path=conf_path))
    return model.flavors[FLAVOR_NAME]


def _load_model_env(path, run_id=None):
    """
        Get ENV file string from a model configuration stored in Python Function format.
        Returned value is a model-relative path to a Conda Environment file,
        or None if none was specified at model save time
    """
    return _load_model_conf(path, run_id).get(ENV, None)


def load_pyfunc(path, run_id=None, suppress_warnings=False):
    """
    Load a model stored in Python function format.

    :param suppress_warnings: If True, non-fatal warning messages associated with the model
                              loading process will be suppressed. If False, these warning messages
                              will be emitted.
    """
    if run_id:
        path = tracking.utils._get_model_log_dir(path, run_id)
    conf = _load_model_conf(path)
    model_py_version = conf.get(PY_VERSION)
    if not suppress_warnings:
        _warn_potentially_incompatible_py_version_if_necessary(model_py_version=model_py_version)
    if CODE in conf and conf[CODE]:
        code_path = os.path.join(path, conf[CODE])
        sys.path = [code_path] + _get_code_dirs(code_path) + sys.path
    data_path = os.path.join(path, conf[DATA]) if (DATA in conf) else path
    return importlib.import_module(conf[MAIN]).load_pyfunc(data_path)


def _warn_potentially_incompatible_py_version_if_necessary(model_py_version):
    if model_py_version is None:
        eprint("The specified model does not have a specified Python version. It may be"
               " incompatible with the version of Python that is currently running:"
               " Python {version}".format(
                   version=PYTHON_VERSION))
    elif get_major_minor_py_version(model_py_version) != get_major_minor_py_version(PYTHON_VERSION):
        eprint("The version of Python that the model was saved in, Python {model_version}, differs"
               " from the version of Python that is currently running, Python {system_version},"
               " and may be incompatible".format(
                   model_version=model_py_version, system_version=PYTHON_VERSION))


def _get_code_dirs(src_code_path, dst_code_path=None):
    if not dst_code_path:
        dst_code_path = src_code_path
    return [(os.path.join(dst_code_path, x))
            for x in os.listdir(src_code_path) if not x.endswith(".py") and not
            x.endswith(".pyc") and not x == "__pycache__"]


def spark_udf(spark, path, run_id=None, result_type="double"):
    """
    Return a Spark UDF that can be used to invoke the Python function formatted model.

    Parameters passed to the UDF are forwarded to the model as a DataFrame where the names are
    simply ordinals (0, 1, ...).

    Example:

    .. code:: python

        predict = mlflow.pyfunc.spark_udf(spark, "/my/local/model")
        df.withColumn("prediction", predict("name", "age")).show()

    :param spark: A SparkSession object.
    :param path: A path containing a pyfunc model.
    :param run_id: ID of the run that produced this model. If provided, ``run_id`` is used to
                   retrieve the model logged with MLflow.
    :param result_type: Spark UDF type returned by the model's prediction method. Default double.

    """

    # Scope Spark import to this method so users don't need pyspark to use non-Spark-related
    # functionality.
    from mlflow.pyfunc.spark_model_cache import SparkModelCache
    from pyspark.sql.functions import pandas_udf

    if run_id:
        path = tracking.utils._get_model_log_dir(path, run_id)

    archive_path = SparkModelCache.add_local_model(spark, path)

    def predict(*args):
        model = SparkModelCache.get_or_load(archive_path)
        schema = {str(i): arg for i, arg in enumerate(args)}
        # Explicitly pass order of columns to avoid lexicographic ordering (i.e., 10 < 2)
        columns = [str(i) for i, _ in enumerate(args)]
        pdf = pandas.DataFrame(schema, columns=columns)
        result = model.predict(pdf)
        return pandas.Series(result)

    return pandas_udf(predict, result_type)


def _copy_file_or_tree(src, dst, dst_dir):
    name = os.path.join(dst_dir, os.path.basename(os.path.abspath(src)))
    if dst_dir:
        os.mkdir(os.path.join(dst, dst_dir))
    if os.path.isfile(src):
        shutil.copy(src=src, dst=os.path.join(dst, name))
    else:
        shutil.copytree(src=src, dst=os.path.join(dst, name))
    return name


def save_model(dst_path, loader_module, data_path=None, code_path=(), conda_env=None,
               model=Model()):
    """
    Export model as a generic Python function model.

    :param dst_path: Path where the model is stored.
    :param loader_module: The module to be used to load the model.
    :param data_path: Path to a file or directory containing model data.
    :param code_path: List of paths (file or dir) contains code dependencies not present in
                      the environment. Every path in the ``code_path`` is added to the Python
                      path before the model is loaded.
    :param conda_env: Path to the Conda environment definition. This environment is activated
                      prior to running model code.
    :return: Model configuration containing model info.

    """
    if os.path.exists(dst_path):
        raise Exception("Path '{}' already exists".format(dst_path))
    os.makedirs(dst_path)
    code = None
    data = None
    env = None

    if data_path:
        model_file = _copy_file_or_tree(src=data_path, dst=dst_path, dst_dir="data")
        data = model_file

    if code_path:
        for path in code_path:
            _copy_file_or_tree(src=path, dst=dst_path, dst_dir="code")
        code = "code"

    if conda_env:
        shutil.copy(src=conda_env, dst=os.path.join(dst_path, "mlflow_env.yml"))
        env = "mlflow_env.yml"

    add_to_model(model, loader_module=loader_module, code=code, data=data, env=env)
    model.save(os.path.join(dst_path, 'MLmodel'))
    return model


def log_model(artifact_path, **kwargs):
    """
    Export model in Python function form and log it with current MLflow tracking service.

    Model is exported by calling :py:meth:`save_model` and logging the result with
    :py:meth:`mlflow.tracking.log_artifacts`.
    """
    with TempDir() as tmp:
        local_path = tmp.path(artifact_path)
        run_id = active_run().info.run_uuid
        if 'model' in kwargs:
            raise Exception("Unused argument 'model'. log_model creates a new model object")

        save_model(dst_path=local_path, model=Model(artifact_path=artifact_path, run_id=run_id),
                   **kwargs)
        log_artifacts(local_path, artifact_path)


def get_module_loader_src(src_path, dst_path):
    """
    Generate Python source of the model loader.

    Model loader contains ``load_pyfunc`` method with no parameters. It hardcodes model
    loading of the given model into a Python source. This is done so that the exported model has no
    unnecessary dependencies on MLflow or any other configuration file format or parsing library.

    :param src_path: Current path to the model.
    :param dst_path: Relative or absolute path where the model will be stored in the deployment
                     environment.
    :return: Python source code of the model loader as string.

    """
    conf_path = os.path.join(src_path, "MLmodel")
    model = Model.load(conf_path)
    if FLAVOR_NAME not in model.flavors:
        raise Exception("Format '{format}' not found not in {path}.".format(format=FLAVOR_NAME,
                                                                            path=conf_path))
    conf = model.flavors[FLAVOR_NAME]
    update_path = ""
    if CODE in conf and conf[CODE]:
        src_code_path = os.path.join(src_path, conf[CODE])
        dst_code_path = os.path.join(dst_path, conf[CODE])
        code_path = ["os.path.abspath('%s')" % x
                     for x in [dst_code_path] + _get_code_dirs(src_code_path, dst_code_path)]
        update_path = "sys.path = {} + sys.path; ".format("[%s]" % ",".join(code_path))

    data_path = os.path.join(dst_path, conf[DATA]) if (DATA in conf) else dst_path
    return loader_template.format(update_path=update_path, main=conf[MAIN], data_path=data_path)


loader_template = """

import importlib
import os
import sys

def load_pyfunc():
    {update_path}return importlib.import_module('{main}').load_pyfunc('{data_path}')

"""
