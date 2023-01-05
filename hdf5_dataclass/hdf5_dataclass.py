"""Implementation of HDF5-(de)serialisable dataclasses

Supports the following dataclass fields:
- primitives: `int`, `float`, `str`
- Pydantic models
- numpy arrays
- dictionaries of primitive types to keys of any of the above
- optionals of the above
"""

from dataclasses import dataclass

from pathlib import Path
from typing import IO, Any, get_args, TypeGuard, Type, get_origin
from typing_extensions import dataclass_transform

import numpy as np
import h5py

from . import utils


FileType = str | Path | IO[bytes]


def _is_supported_dict(T: type) -> bool:
    if not get_origin(T) == dict:
        return False
    K, V = get_args(T)
    return utils.is_primitive(K) and _is_type_supported(V)


def _is_type_supported(T: type) -> bool:
    return (
        utils.is_primitive(T)
        or (
            utils.is_optional(T)
            and _is_type_supported(utils.extract_type_from_optional(T))
        )
        or utils.is_numpy_array(T)
        or utils.is_pydantic_model(T)
        or _is_supported_dict(T)
        or is_hdf5_dataclass(T)
    )


def is_hdf5_dataclass(T: type) -> TypeGuard[Type["HDF5Dataclass"]]:
    """Check whether a given type is a subclass of HDF5Dataclass

    Returns:
        TypeGuard: bool-convertable TypeGuard
    """
    return issubclass(T, HDF5Dataclass)


@dataclass_transform()
class HDF5Dataclass:
    """Base class for a dataclass with the ability to be hdf5-(de)serialised.

    Raises:
        AssertionError: when not all of the fields are serialisable
    """

    serialisable_attrs: dict[str, type]

    def __init_subclass__(cls, **kwargs):
        dataclass_cls = dataclass(cls, **kwargs)
        serialisable_attrs = utils.fields(dataclass_cls)
        unsupported_attrs = [
            attr for attr, T in serialisable_attrs.items() if not _is_type_supported(T)
        ]
        assert (
            not unsupported_attrs
        ), f"Types of attributes {', '.join(unsupported_attrs)} are not supported!"

        dataclass_cls.serialisable_attrs = serialisable_attrs
        return dataclass_cls

    def to_hdf5(self, output: FileType | h5py.File | h5py.Group):
        """Serialise an object to `output`.

        Use it either to create a new HDF5 file or add to an existing HDF5 File/Group.

        Args:
            output (FileType | h5py.File | h5py.Group): output file/HDF5 group
        """

        def serialise_single(
            attr: str, val: Any, T: type, h5: h5py.File | h5py.Group
        ) -> None:
            if val is None:
                return

            if utils.is_primitive(T) or utils.is_optional_primitive(T):
                h5.attrs[attr] = val
            elif utils.is_pydantic_model(T):
                h5.attrs[attr] = val.json()
            # TODO: elif list - json?
            elif utils.is_numpy_array(T):
                h5.create_dataset(attr, data=val)
            elif is_hdf5_dataclass(T):
                grp = h5.create_group(attr)
                val.to_hdf5(output=grp)
            elif _is_supported_dict(T):
                _, V = get_args(T)
                grp = h5.create_group(attr)
                for k, v in val.items():
                    serialise_single(k, v, V, grp)
            else:
                raise Exception(f"Unsupported type of attribute '{attr}'")

        h5 = (
            output
            if isinstance(output, (h5py.File, h5py.Group))
            else h5py.File(output, "w")
        )

        for attr, T in self.serialisable_attrs.items():
            val = getattr(self, attr)
            serialise_single(attr, val, T, h5)

    @classmethod
    def from_hdf5(cls, input: FileType | h5py.File | h5py.Group):
        """Deserialise an object of class `cls` from a given `input` file or a HDF5 File/Group

        Args:
            input (FileType | h5py.File | h5py.Group): input file/HDF5 group
        """

        def deserialise_single(attr: str, T: type, h5: h5py.File | h5py.Group) -> Any:
            val = None
            if utils.is_primitive(T):
                val = h5.attrs.get(attr)
                assert (
                    val is not None
                ), f"Attribute '{attr}' marked as non-optional, but value is not present!"
            elif utils.is_optional_primitive(T):
                val = h5.attrs.get(attr)
            elif utils.is_pydantic_model(T):
                val = T.parse_raw(h5.attrs.get(attr))
            else:
                serialised = h5[attr]
                if isinstance(serialised, h5py.Dataset):
                    val = np.array(serialised)
                elif isinstance(serialised, h5py.Group):
                    assert is_hdf5_dataclass(T) or _is_supported_dict(T)
                    if is_hdf5_dataclass(T):
                        val = T.from_hdf5(serialised)
                    else:
                        # dict case
                        _, V = get_args(T)
                        val = {}
                        keys = (
                            serialised.attrs.keys()
                            if utils.is_primitive(V) or utils.is_optional_primitive(V)
                            else serialised.keys()
                        )
                        for key in keys:
                            val[key] = deserialise_single(key, V, serialised)
                else:
                    raise Exception("Unknown type of data in hdf5")
            return val

        h5 = (
            input
            if isinstance(input, (h5py.File, h5py.Group))
            else h5py.File(input, "r")
        )

        attrs = {}
        for attr, T in cls.serialisable_attrs.items():
            attrs[attr] = deserialise_single(attr, T, h5)
        return cls(**attrs)
