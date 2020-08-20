# Copyright 2018-2020 Streamlit Inc.
# Author: Dominik Moritz
# Everything in this module is copied from Streamlit and have minor
# modifications to use newer libraries and make code cleaner.

"""
This module exposes methods to find reference variables for a code
object. It exposes `get_code_context` method to package different
variables required to find the references and `get_referenced_objects`
which uses the code context to find the references.

CodeHasher uses these methods as part of hashing an entity function for
Bionic cache invalidation. Using these methods, CodeHasher can hash the
references of entity functions (and even their references), so that any
change in the references are also detected by Bionic to invalidate
cache.
"""

import attr
import dis
import importlib
import inspect

from .exception import CodeVersioningError
from .utils.misc import oneline


@attr.s
class CodeContext:
    """
    Holds variable information from function and code object
    attributes.

    Attributes
    ----------
    globals: dict
        A reference to the dictionary that holds the function’s global
        variables — the global namespace of the module in which the
        function was defined.
    cells: dict
        A dictionary that tracks all free variables by name and cell
        variable name by itself.
    varnames: dict from TaskKey to Task
        A dictionary that is used to track local variables by name.
    """

    globals = attr.ib()
    cells = attr.ib()
    varnames = attr.ib()


def get_code_context(func) -> CodeContext:
    code = func.__code__

    # Mapping from variable name to the value if we can resolve it.
    # Otherwise map to the name.
    cells = {}

    for var in code.co_cellvars:
        cells[var] = var  # Instead of value, we use the name.

    if code.co_freevars:
        assert len(code.co_freevars) == len(func.__closure__)
        for freevar, cell in zip(code.co_freevars, func.__closure__):
            cells[freevar] = cell.cell_contents

    varnames = {}
    if inspect.ismethod(func):
        varnames = {"self": func.__self__}

    return CodeContext(globals=func.__globals__, cells=cells, varnames=varnames)


def get_referenced_objects(code, context):
    """
    Returns referenced objects for a code object.

    Referenced objects can be anything from a class, a function, a module or any
    variables. The references can also be any scope, like global, local, cell,
    free, or can even be in another object referenced using a full qualified
    name.

    Note that this method cannot find references when the reference depends on
    the result of a function call. It cannot detect such references because we
    don’t want to call random functions during the time of detection, because
    doing so might be expensive and can have unintended consequences.
    """
    # Top of the stack
    tos = None
    lineno = None
    refs = []

    def set_tos(t):
        nonlocal tos
        if tos is not None:
            # Hash tos so we support reading multiple objects
            refs.append(tos)
        tos = t

    # Our goal is to find referenced objects. The problem is that co_names
    # does not have full qualified names in it. So if you access `foo.bar`,
    # co_names has `foo` and `bar` in it but it doesn't tell us that the
    # code reads `bar` of `foo`. We are going over the bytecode to resolve
    # from which object an attribute is requested.
    # Read more about bytecode at https://docs.python.org/3/library/dis.html

    for op in dis.get_instructions(code):
        try:
            # Sometimes starts_line is None, in which case let's just remember the
            # previous start_line (if any). This way when there's an exception we at
            # least can point users somewhat near the line where the error stems from.
            if op.starts_line is not None:
                lineno = op.starts_line

            if op.opname in ["LOAD_GLOBAL", "LOAD_NAME"]:
                if op.argval in context.globals:
                    set_tos(context.globals[op.argval])
                else:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_DEREF", "LOAD_CLOSURE"]:
                set_tos(context.cells[op.argval])
            elif op.opname == "IMPORT_NAME":
                try:
                    set_tos(importlib.import_module(op.argval))
                except ImportError:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_METHOD", "LOAD_ATTR", "IMPORT_FROM"]:
                if tos is None:
                    refs.append(op.argval)
                elif isinstance(tos, str):
                    tos += "." + op.argval
                else:
                    tos = getattr(tos, op.argval)
            elif op.opname == "DELETE_FAST" and tos:
                del context.varnames[op.argval]
                tos = None
            elif op.opname == "STORE_FAST" and tos:
                context.varnames[op.argval] = tos
                tos = None
            elif op.opname == "LOAD_FAST" and op.argval in context.varnames:
                set_tos(context.varnames[op.argval])
            else:
                # For all other instructions, hash the current TOS.
                if tos is not None:
                    refs.append(tos)
                    tos = None
        except Exception as e:
            message = oneline(
                f"""
            Bionic found a code reference in file ${code.co_filename}
            at line ${lineno} that it cannot hash when hashing
            ${code.co_name}.This should be impossible and is most
            likely a bug in Bionic. Please raise a new issue at
            https://github.com/square/bionic/issues to let us know.
            """
            )
            raise CodeVersioningError(message) from e

    return refs
