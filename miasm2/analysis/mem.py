"""This module provides classes to manipulate C structures backed by a VmMngr
object (a miasm sandbox virtual memory).

The main idea is to declare the fields of the structure in the class:

    # FIXME: "I" => "u32"
    class MyStruct(PinnedStruct):
        fields = [
            # Integer field: just struct.pack fields with one value
            ("num", Num("I")),
            ("flags", Num("B")),
            # Ptr fields are Num, but they can also be dereferenced
            # (self.deref_<field>). Deref can be read and set.
            ("other", Ptr("I", OtherStruct)),
            # Ptr to a variable length String
            ("s", Ptr("I", PinnedStr)),
            ("i", Ptr("I", Num("I"))),
        ]

And access the fields:

    mstruct = MyStruct(jitter.vm, addr)
    mstruct.num = 3
    assert mstruct.num == 3
    mstruct.other = addr2
    mstruct.deref_other = OtherStruct(jitter.vm, addr)

The `addr` argument can be omited if an allocator is set, in which case the
structure will be automatically allocated in memory:

    my_heap = miasm2.os_dep.common.heap()
    # the allocator is a func(VmMngr) -> integer_address
    set_allocator(my_heap)

Note that some structures (e.g. PinnedStr or PinnedArray) do not have a static
size and cannot be allocated automatically.


As you saw previously, to use this module, you just have to inherit from
PinnedStruct and define a list of (<field_name>, <field_definition>). Available
Type classes are:

    - Num: for number (float or int) handling
    - RawStruct: abstraction over a simple struct pack/unpack
    - Ptr: a pointer to another PinnedType instance
    - FIXME: TODEL Inline: include another PinnedStruct as a field (equivalent to having a
      struct field into another struct in C)
    - Array: a fixed size array of Types (points)
    - Union: similar to `union` in C, list of Types at the same offset in a
      structure; the union has the size of the biggest Type
    - BitField: similar to C bitfields, a list of
      [(<field_name), (number_of_bits)]; creates fields that correspond to
      certain bits of the field

A Type always has a fixed size in memory.


Some special memory structures are already implemented; they all are subclasses
of PinnedType with a custom implementation:

    - PinnedSelf: this class is just a special marker to reference a
      PinnedStruct subclass inside itself. Works with Ptr and Array (e.g.
      Ptr(_, PinnedSelf) for a pointer the same type as the class who uses this
      kind of field)
    - PinnedVoid: empty PinnedType, placeholder to be casted to an implemented
      PinnedType subclass
    - PinnedStr: represents a string in memory; the encoding can be passed to the
      constructor (null terminated ascii/ansi or null terminated utf16)
    - PinnedArray: an unsized array of Type; unsized here means that there is
      no defined sized for this array, equivalent to a int* or char*-style table
      in C. It cannot be allocated automatically, since it has no known size
    - PinnedSizedArray: a sized PinnedArray, can be automatically allocated in memory
      and allows more operations than PinnedArray
    - pin: a function that dynamically generates a PinnedStruct subclass from a
      Type. This class has only one field named "value".

A PinnedType do not always have a static size (cls.sizeof()) nor a dynamic size
(self.get_size()).
"""

import logging
import struct

log = logging.getLogger(__name__)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(levelname)-5s: %(message)s"))
log.addHandler(console_handler)
log.setLevel(logging.WARN)

# ALLOCATOR is a function(vm, size) -> allocated_address
# TODO: as a PinnedType class attribute
ALLOCATOR = None

# Cache for dynamically generated PinnedTypes
DYN_MEM_STRUCT_CACHE = {}

def set_allocator(alloc_func):
    """Set an allocator for this module; allows to instanciate statically sized
    PinnedTypes (i.e. sizeof() is implemented) without specifying the address
    (the object is allocated by @alloc_func in the vm.

    @alloc_func: func(VmMngr) -> integer_address
    """
    global ALLOCATOR
    ALLOCATOR = alloc_func


# Helpers

def indent(s, size=4):
    """Indent a string with @size spaces"""
    return ' '*size + ('\n' + ' '*size).join(s.split('\n'))


# FIXME: copied from miasm2.os_dep.common and fixed
def get_str_ansi(vm, addr, max_char=None):
    """Get a null terminated ANSI encoded string from a VmMngr.

    @vm: VmMngr instance
    @max_char: max number of characters to get in memory
    """
    l = 0
    tmp = addr
    while ((max_char is None or l < max_char) and
           vm.get_mem(tmp, 1) != "\x00"):
        tmp += 1
        l += 1
    return vm.get_mem(addr, l).decode("latin1")


# TODO: get_raw_str_utf16 for length calculus
def get_str_utf16(vm, addr, max_char=None):
    """Get a (double) null terminated utf16 little endian encoded string from
    a VmMngr. This encoding is mainly used in Windows.

    FIXME: the implementation do not work with codepoints that are encoded on
    more than 2 bytes in utf16.

    @vm: VmMngr instance
    @max_char: max number of bytes to get in memory
    """
    l = 0
    tmp = addr
    # TODO: test if fetching per page rather than 2 byte per 2 byte is worth it?
    while ((max_char is None or l < max_char) and
           vm.get_mem(tmp, 2) != "\x00\x00"):
        tmp += 2
        l += 2
    s = vm.get_mem(addr, l)
    return s.decode('utf-16le')


def set_str_ansi(vm, addr, s):
    """Encode a string to null terminated ascii/ansi and set it in a VmMngr
    memory.

    @vm: VmMngr instance
    @addr: start address to serialize the string to
        s: the str to serialize
    """
    vm.set_mem(addr, s + "\x00")


def set_str_utf16(vm, addr, s):
    """Same as set_str_ansi with (double) null terminated utf16 encoding."""
    s = (s + '\x00').encode('utf-16le')
    vm.set_mem(addr, s)


# Type to PinnedType helper

def pin(field):
    """Generate a PinnedStruct subclass from a field. The field's value can
    be accessed through self.value or self.deref_value if field is a Ptr.

    @field: a Type instance.
    """
    if field in DYN_MEM_STRUCT_CACHE:
        return DYN_MEM_STRUCT_CACHE[field]

    fields = [("value", field)]
    # Build a type to contain the field type
    mem_type = type("Pinned%r" % field, (PinnedStruct,), {'fields': fields})
    DYN_MEM_STRUCT_CACHE[field] = mem_type
    return mem_type


# Type classes

class Type(object):
    """Base class to provide methods to set and get fields from virtual pin.

    Subclasses can either override _pack and _unpack, or get and set if data
    serialization requires more work (see Inline implementation for an example).
    """

    _self_type = None
    _fields = []

    def _pack(self, val):
        """Serializes the python value @val to a raw str"""
        raise NotImplementedError()

    def _unpack(self, raw_str):
        """Deserializes a raw str to an object representing the python value
        of this field.
        """
        raise NotImplementedError()

    def set(self, vm, addr, val):
        """Set a VmMngr memory from a value.

        @vm: VmMngr instance
        @addr: the start adress in memory to set
        @val: the python value to serialize in @vm at @addr
        """
        raw = self._pack(val)
        vm.set_mem(addr, raw)

    def get(self, vm, addr):
        """Get the python value of a field from a VmMngr memory at @addr."""
        raw = vm.get_mem(addr, self.size())
        return self._unpack(raw)

    def _get_self_type(self):
        return self._self_type

    def _set_self_type(self, self_type):
        """If this field refers to PinnedSelf, replace it with @self_type (a
        PinnedType subclass) when using it. Generally not used outside the lib.
        """
        self._self_type = self_type

    def size(self):
        """Return the size in bytes of the serialized version of this field"""
        raise NotImplementedError()

    def __len__(self):
        return self.size()

    def __neq__(self, other):
        return not self == other


class RawStruct(Type):
    """Dumb struct.pack/unpack field. Mainly used to factorize code.

    Value is a tuple corresponding to the struct @fmt passed to the constructor.
    """

    def __init__(self, fmt):
        self._fmt = fmt

    def _pack(self, fields):
        return struct.pack(self._fmt, *fields)

    def _unpack(self, raw_str):
        return struct.unpack(self._fmt, raw_str)

    def size(self):
        return struct.calcsize(self._fmt)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self._fmt)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and self._fmt == other._fmt

    def __hash__(self):
        return hash((self.__class__, self._fmt))


class Num(RawStruct):
    """Represents a number (integer or float). The number is encoded with
    a struct-style format which must represent only one value.

    TODO: use u32, i16, etc. for format.
    """

    def _pack(self, number):
        return super(Num, self)._pack([number])

    def _unpack(self, raw_str):
        upck = super(Num, self)._unpack(raw_str)
        if len(upck) != 1:
            raise ValueError("Num format string unpacks to multiple values, "
                             "should be 1")
        return upck[0]


class Ptr(Num):
    """Special case of number of which value indicates the address of a
    PinnedType. Provides deref_<field> as well as <field> when used, to set and
    get the pointed PinnedType.
    """

    def __init__(self, fmt, dst_type, *type_args, **type_kwargs):
        """
        @fmt: (str) Num compatible format that will be the Ptr representation
            in memory
        @dst_type: (PinnedType or Type) the PinnedType this Ptr points to.
            If a Type is given, it is transformed into a PinnedType with
            pin(TheType).
        *type_args, **type_kwargs: arguments to pass to the the pointed
            PinnedType when instanciating it (e.g. for PinnedStr encoding or
            PinnedArray field_type).
        """
        if (not isinstance(dst_type, Type) and
                not (isinstance(dst_type, type) and
                        issubclass(dst_type, PinnedType)) and
                not dst_type == PinnedSelf):
            raise ValueError("dst_type of Ptr must be a PinnedType type, a "
                             "Type instance, the PinnedSelf marker or a class "
                             "name.")
        super(Ptr, self).__init__(fmt)
        if isinstance(dst_type, Type):
            # Patch the field to propagate the PinnedSelf replacement
            dst_type._get_self_type = lambda: self._get_self_type()
            # dst_type cannot be patched here, since _get_self_type of the outer
            # class has not yet been set. Patching dst_type involves calling
            # pin(dst_type), which will only return a type that does not point
            # on PinnedSelf but on the right class only when _get_self_type of the
            # outer class has been replaced by _MetaPinnedStruct.
            # In short, dst_type = pin(dst_type) is not valid here, it is done
            # lazily in _fix_dst_type
        self._dst_type = dst_type
        self._type_args = type_args
        self._type_kwargs = type_kwargs

    def _fix_dst_type(self):
        if self._dst_type == PinnedSelf:
            if self._get_self_type() is not None:
                self._dst_type = self._get_self_type()
            else:
                raise ValueError("Unsupported usecase for PinnedSelf, sorry")
        if isinstance(self._dst_type, Type):
            self._dst_type = pin(self._dst_type)

    @property
    def dst_type(self):
        """Return the type (PinnedType subtype) this Ptr points to."""
        self._fix_dst_type()
        return self._dst_type

    def deref_get(self, vm, addr):
        """Deserializes the data in @vm (VmMngr) at @addr to self.dst_type.
        Equivalent to a pointer dereference rvalue in C.
        """
        return self.dst_type(vm, addr, *self._type_args, **self._type_kwargs)

    def deref_set(self, vm, addr, val):
        """Serializes the @val PinnedType subclass instance in @vm (VmMngr) at
        @addr. Equivalent to a pointer dereference assignment in C.
        """
        # Sanity check
        if self.dst_type != val.__class__:
            log.warning("Original type was %s, overriden by value of type %s",
                        self._dst_type.__name__, val.__class__.__name__)

        # Actual job
        vm.set_mem(addr, str(val))

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self._dst_type)

    def __eq__(self, other):
        return super(Ptr, self).__eq__(other) and \
                self.dst_type == other.dst_type and \
                self._type_args == other._type_args and \
                self._type_kwargs == other._type_kwargs

    def __hash__(self):
        return hash((super(Ptr, self).__hash__(), self.dst_type,
            self._type_args))


class Inline(Type):
    """Field used to inline a PinnedType in another PinnedType. Equivalent to
    having a struct field in a C struct.

    Concretely:

        class MyStructClass(PinnedStruct):
            fields = [("f1", Num("I")), ("f2", Num("I"))]

        class Example(PinnedStruct):
            fields = [("mystruct", Inline(MyStructClass))]

        ex = Example(vm, addr)
        ex.mystruct.f2 = 3 # inlined structure field access
        ex.mystruct = MyStructClass(vm, addr2) # struct copy

    It can be seen like a bridge to use a PinnedStruct as a Type

    TODO: make the Inline implicit when setting a field to be a PinnedStruct
    """

    def __init__(self, inlined_type, *type_args, **type_kwargs):
        if not issubclass(inlined_type, PinnedStruct):
            raise ValueError("inlined type if Inline must be a PinnedStruct")
        self._il_type = inlined_type
        self._type_args = type_args
        self._type_kwargs = type_kwargs

    def set(self, vm, addr, val):
        raw = str(val)
        vm.set_mem(addr, raw)

    def get(self, vm, addr):
        return self._il_type(vm, addr)

    def size(self):
        return self._il_type.sizeof()

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self._il_type)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
                self._il_type == other._il_type and \
                self._type_args == other._type_args and \
                self._type_kwargs == other._type_kwargs

    def __hash__(self):
        return hash((self.__class__, self._il_type, self._type_args))


class Array(Type):
    """A fixed size array (contiguous sequence) of a Type subclass
    elements. Similar to something like the char[10] type in C.

    Getting an array field actually returns a PinnedSizedArray. Setting it is
    possible with either a list or a PinnedSizedArray instance. Examples of syntax:

        class Example(PinnedStruct):
            fields = [("array", Array(Num("B"), 4))]

        mystruct = Example(vm, addr)
        mystruct.array[3] = 27
        mystruct.array = [1, 4, 8, 9]
        mystruct.array = PinnedSizedArray(vm, addr2, Num("B"), 4)
    """

    def __init__(self, field_type, array_len):
        self.field_type = field_type
        self.array_len = array_len

    def _set_self_type(self, self_type):
        super(Array, self)._set_self_type(self_type)
        self.field_type._set_self_type(self_type)

    def set(self, vm, addr, val):
        # PinnedSizedArray assignment
        if isinstance(val, PinnedSizedArray):
            if val.array_len != self.array_len or len(val) != self.size():
                raise ValueError("Size mismatch in PinnedSizedArray assignment")
            raw = str(val)
            vm.set_mem(addr, raw)

        # list assignment
        elif isinstance(val, list):
            if len(val) != self.array_len:
                raise ValueError("Size mismatch in PinnedSizedArray assignment ")
            offset = 0
            for elt in val:
                self.field_type.set(vm, addr + offset, elt)
                offset += self.field_type.size()

        else:
            raise RuntimeError(
                "Assignment only implemented for list and PinnedSizedArray")

    def get(self, vm, addr):
        return PinnedSizedArray(vm, addr, self.field_type, self.array_len)

    def size(self):
        return self.field_type.size() * self.array_len

    def __repr__(self):
        return "%r[%s]" % (self.field_type, self.array_len)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
                self.field_type == other.field_type and \
                self.array_len == other.array_len

    def __hash__(self):
        return hash((self.__class__, self.field_type, self.array_len))


class Union(Type):
    """Allows to put multiple fields at the same offset in a PinnedStruct, similar
    to unions in C. The Union will have the size of the largest of its fields.

    Example:

        class Example(PinnedStruct):
            fields = [("uni", Union([
                                  ("f1", Num("<B")),
                                  ("f2", Num("<H"))
                              ])
                     )]

        ex = Example(vm, addr)
        ex.f2 = 0x1234
        assert ex.f1 == 0x34
        assert ex.uni == '\x34\x12'
        assert ex.get_addr("f1") == ex.get_addr("f2")
    """

    def __init__(self, field_list):
        """field_list is a [(name, field)] list, see the class doc"""
        self.field_list = field_list

    def size(self):
        return max(field.size() for _, field in self.field_list)

    def set(self, vm, addr, val):
        if not isinstance(val, str) or not len(str) == self.size():
            raise ValueError("Union can only be set with raw str of the Union's"
                             " size")
        vm.set_mem(vm, addr, val)

    def get(self, vm, addr):
        return vm.get_mem(addr, self.size())

    def __repr__(self):
        fields_repr = ', '.join("%s: %r" % (name, field)
                                for name, field in self.field_list)
        return "%s(%s)" % (self.__class__.__name__, fields_repr)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
                self.field_list == other.field_list

    def __hash__(self):
        return hash((self.__class__, tuple(self.field_list)))


class Bits(Type):
    """Helper class for BitField, not very useful on its own. Represents some
    bits of a Num.

    The @backing_num is used to know how to serialize/deserialize data in vm,
    but getting/setting this fields only affects bits from @bit_offset to
    @bit_offset + @bits. Masking and shifting is handled by the class, the aim
    is to provide a transparent way to set and get some bits of a num.
    """

    def __init__(self, backing_num, bits, bit_offset):
        if not isinstance(backing_num, Num):
            raise ValueError("backing_num should be a Num instance")
        self._num = backing_num
        self._bits = bits
        self._bit_offset = bit_offset

    def set(self, vm, addr, val):
        val_mask = (1 << self._bits) - 1
        val_shifted = (val & val_mask) << self._bit_offset
        num_size = self._num.size() * 8

        full_num_mask = (1 << num_size) - 1
        num_mask = (~(val_mask << self._bit_offset)) & full_num_mask

        num_val = self._num.get(vm, addr)
        res_val = (num_val & num_mask) | val_shifted
        self._num.set(vm, addr, res_val)

    def get(self, vm, addr):
        val_mask = (1 << self._bits) - 1
        num_val = self._num.get(vm, addr)
        res_val = (num_val >> self._bit_offset) & val_mask
        return res_val

    def size(self):
        return self._num.size()

    @property
    def bit_size(self):
        """Number of bits read/written by this class"""
        return self._bits

    @property
    def bit_offset(self):
        """Offset in bits (beginning at 0, the LSB) from which to read/write
        bits.
        """
        return self._bit_offset

    def __repr__(self):
        return "%s%r(%d:%d)" % (self.__class__.__name__, self._num,
                                self._bit_offset, self._bit_offset + self._bits)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
                self._num == other._num and self._bits == other._bits and \
                self._bit_offset == other._bit_offset

    def __hash__(self):
        return hash((self.__class__, self._num, self._bits, self._bit_offset))


class BitField(Union):
    """A C-like bitfield.

    Constructed with a list [(<field_name>, <number_of_bits>)] and a
    @backing_num. The @backing_num is a Num instance that determines the total
    size of the bitfield and the way the bits are serialized/deserialized (big
    endian int, little endian short...). Can be seen (and implemented) as a
    Union of Bits fields.

    Creates fields that allow to access the bitfield fields easily. Example:

        class Example(PinnedStruct):
            fields = [("bf", BitField(Num("B"), [
                                ("f1", 2),
                                ("f2", 4),
                                ("f3", 1)
                             ])
                     )]

        ex = Example(vm, addr)
        ex.memset()
        ex.f2 = 2
        ex.f1 = 5 # 5 does not fit on two bits, it will be binarily truncated
        assert ex.f1 == 3
        assert ex.f2 == 2
        assert ex.f3 == 0 # previously memset()
        assert ex.bf == 3 + 2 << 2
    """

    def __init__(self, backing_num, bit_list):
        """@backing num: Num intance, @bit_list: [(name, n_bits)]"""
        self._num = backing_num
        fields = []
        offset = 0
        for name, bits in bit_list:
            fields.append((name, Bits(self._num, bits, offset)))
            offset += bits
        if offset > self._num.size() * 8:
            raise ValueError("sum of bit lengths is > to the backing num size")
        super(BitField, self).__init__(fields)

    def set(self, vm, addr, val):
        self._num.set(vm, addr, val)

    def get(self, vm, addr):
        return self._num.get(vm, addr)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and \
                self._num == other._num and super(BitField, self).__eq__(other)

    def __hash__(self):
        return hash((super(BitField, self).__hash__(), self._num))


# PinnedType classes

class _MetaPinnedType(type):
    def __repr__(cls):
        return cls.__name__


class _MetaPinnedStruct(_MetaPinnedType):
    """PinnedStruct metaclass. Triggers the magic that generates the class fields
    from the cls.fields list.

    Just calls PinnedStruct.gen_fields(), the actual implementation can seen be
    there.
    """

    def __init__(cls, name, bases, dct):
        super(_MetaPinnedStruct, cls).__init__(name, bases, dct)
        cls.gen_fields()


class PinnedType(object):
    __metaclass__ = _MetaPinnedType

    _size = None

    # Classic usage methods

    def __init__(self, vm, addr=None, *args, **kwargs):
        global ALLOCATOR
        super(PinnedType, self).__init__(*args, **kwargs)
        self._vm = vm
        if addr is None:
            if ALLOCATOR is None:
                raise ValueError("Cannot provide None address to PinnedType() if"
                                 "%s.set_allocator has not been called."
                                 % __name__)
            self._addr = ALLOCATOR(vm, self.get_size())
        else:
            self._addr = addr

    def get_addr(self, field=None):
        """Return the address of this PinnedType or one of its fields.

        @field: (str, optional) used by subclasses to specify the name or index
            of the field to get the address of
        """
        return self._addr

    @classmethod
    def sizeof(cls):
        """ABSTRACT Return the static size of this type.
        """
        raise NotImplementedError("Abstract")

    def get_size(self):
        """Return the dynamic size of this structure (e.g. the size of an
        instance). Defaults to sizeof for this base class.

        For example, PinnedSizedArray defines get_size but not sizeof, as an
        instance has a fixed size (because it has a fixed length and
        field_type), but all the instance do not have the same size.
        """
        return self.sizeof()

    def memset(self, byte='\x00'):
        """Fill the memory space of this PinnedType with @byte ('\x00' by
        default). The size is retrieved with self.get_size() (dynamic size).
        """
        # TODO: multibyte patterns
        if not isinstance(byte, str) or not len(byte) == 1:
            raise ValueError("byte must be a 1-lengthed str")
        self._vm.set_mem(self.get_addr(), byte * self.get_size())

    def cast(self, other_type, *type_args, **type_kwargs):
        """Cast this PinnedType to another PinnedType (same address, same vm, but
        different type). Return the casted PinnedType.
        """
        return other_type(self._vm, self.get_addr(), *type_args, **type_kwargs)

    def cast_field(self, field, other_type, *type_args, **type_kwargs):
        """ABSTRACT: Same as cast, but the address of the returned PinnedType
        is the address at which @field is in the current PinnedType.

        @field: field specification, for example its name for a struct, or an
            index in an array. See the subclass doc.
        """
        raise NotImplementedError("Abstract")

    def raw(self):
        """Raw binary (str) representation of the PinnedType as it is in
        memory.
        """
        return self._vm.get_mem(self.get_addr(), self.get_size())

    def __len__(self):
        return self.get_size()

    def __str__(self):
        return self.raw()

    def __repr__(self):
        attrs = sorted(self._attrs.iteritems(), key=lambda a: a[1]["offset"])
        out = []
        for name, attr in attrs:
            field = attr["field"]
            val_repr = repr(self.get_field(name))
            if '\n' in val_repr:
                val_repr = '\n' + indent(val_repr, 4)
            out.append("%s: %r = %s" % (name, field, val_repr))
        return '%r:\n' % self.__class__ + indent('\n'.join(out), 2)

    def __eq__(self, other):
        return self.__class__ == other.__class__ and str(self) == str(other)

    def __ne__(self, other):
        return not self == other


class PinnedStruct(PinnedType):
    """Base class to implement VmMngr backed C-like structures in miasm.

    The mechanism is the following:
        - set a "fields" class field to be a list of
          (<field_name (str)>, <Type_subclass_instance>)
        - instances of this class will have properties to interract with these
          fields.

    Example:
        class Example(PinnedStruct):
            fields = [
                # Number field: just struct.pack fields with one value
                ("num", Num("I")),
                ("flags", Num("B")),
                # Ptr fields are Num, but they can also be dereferenced
                # (self.deref_<field>). Deref can be read and set.
                ("other", Ptr("I", OtherStruct)),
                ("i", Ptr("I", Num("I"))),
                # Ptr to a variable length String
                ("s", Ptr("I", PinnedStr)),
            ]

        mstruct = MyStruct(vm, addr)

        # Field assignment modifies virtual memory
        mstruct.num = 3
        assert mstruct.num == 3
        memval = struct.unpack("I", vm.get_mem(mstruct.get_addr(),
                                                      4))[0]
        assert memval == mstruct.num

        # Pinnedset sets the whole structure
        mstruct.memset()
        assert mstruct.num == 0
        mstruct.memset('\x11')
        assert mstruct.num == 0x11111111

        other = OtherStruct(vm, addr2)
        mstruct.other = other.get_addr()
        assert mstruct.other == other.get_addr()
        assert mstruct.deref_other == other
        assert mstruct.deref_other.foo == 0x1234

    See the various Type doc for more information.
    """
    __metaclass__ = _MetaPinnedStruct

    fields = []

    @classmethod
    def sizeof(cls):
        # Child classes can set cls._size if their size is not the sum of
        # their fields
        if cls._size is None:
            return sum(a["field"].size() for a in cls._attrs.itervalues())
        return cls._size

    def get_addr(self, field_name=None):
        """
        @field_name: (str, optional) the name of the field to get the
            address of
        """
        if field_name is not None:
            if field_name not in self._attrs:
                raise ValueError("This structure has no %s field" % field_name)
            offset = self._attrs[field_name]['offset']
        else:
            offset = 0
        return self._addr + offset

    def get_field_type(self, name):
        """return the type subclass instance describing field @name."""
        return self._attrs[name]['field']

    def get_field(self, name):
        """get a field value by name.

        useless most of the time since fields are accessible via self.<name>.
        """
        if name not in self._attrs:
            raise attributeerror("'%s' object has no attribute '%s'"
                                 % (self.__class__.__name__, name))
        field = self._attrs[name]["field"]
        offset = self._attrs[name]["offset"]
        return field.get(self._vm, self.get_addr() + offset)

    def set_field(self, name, val):
        """set a field value by name. @val is the python value corresponding to
        this field type.

        useless most of the time since fields are accessible via self.<name>.
        """
        if name not in self._attrs:
            raise attributeerror("'%s' object has no attribute '%s'"
                                 % (self.__class__.__name__, name))
        field = self._attrs[name]["field"]
        offset = self._attrs[name]["offset"]
        field.set(self._vm, self.get_addr() + offset, val)

    def deref_field(self, name):
        """get the memstruct pointed by <name> field.

        useless most of the time since fields are accessible via
        self.deref_<name>.
        """
        addr = self.get_field(name)
        field = self._attrs[name]["field"]
        assert isinstance(field, Ptr),\
               "programming error: field should be a Ptr"
        return field.deref_get(self._vm, addr)

    def set_deref_field(self, name, val):
        """set the memstruct pointed by <name> field. @val should be of the
        type of the pointed memstruct. the field must be a Ptr.

        useless most of the time since fields are accessible via
        self.deref_<name>.
        """
        addr = self.get_field(name)
        field = self._attrs[name]["field"]
        assert isinstance(field, Ptr),\
               "programming error: field should be a Ptr"
        field.deref_set(self._vm, addr, val)

    def cast_field(self, field, other_type, *type_args, **type_kwargs):
        """
        @field: a field name
        """
        return other_type(self._vm, self.get_addr(field),
                          *type_args, **type_kwargs)


    # Field generation methods, voluntarily public to be able to regen fields
    # after class definition

    @classmethod
    def gen_fields(cls, fields=None):
        """Generate the fields of this class (so that they can be accessed with
        self.<field_name>) from a @fields list, as described in the class doc.

        Useful in case of a type cyclic dependency. For example, the following
        is not possible in python:

            class A(PinnedStruct):
                fields = [("b", Ptr("I", B))]

            class B(PinnedStruct):
                fields = [("a", Ptr("I", A))]

        With gen_fields, the following is the legal equivalent:

            class A(PinnedStruct):
                pass

            class B(PinnedStruct):
                fields = [("a", Ptr("I", A))]

            A.fields = [("b", Ptr("I", B))]
            a.gen_field()
        """
        if fields is None:
            fields = cls.fields
        cls._attrs = {}
        offset = 0
        for name, field in cls.fields:
            # For reflexion
            field._set_self_type(cls)
            cls.gen_field(name, field, offset)
            offset += field.size()
        cls._size = offset

    @classmethod
    def gen_field(cls, name, field, offset):
        """Generate only one field

        @name: (str) the name of the field
        @field: (Type instance) the field type
        @offset: (int) the offset of the field in the structure
        """
        cls._gen_simple_attr(name, field, offset)
        if isinstance(field, Union):
            cls._gen_union_attr(field, offset)

    @classmethod
    def _gen_simple_attr(cls, name, field, offset):
        cls._attrs[name] = {"field": field, "offset": offset}

        # Generate self.<name> getter and setter
        setattr(cls, name, property(
            lambda self: self.get_field(name),
            lambda self, val: self.set_field(name, val)
        ))

        # Generate self.deref_<name> getter and setter if this field is a
        # Ptr
        if isinstance(field, Ptr):
            setattr(cls, "deref_%s" % name, property(
                lambda self: self.deref_field(name),
                lambda self, val: self.set_deref_field(name, val)
            ))

    @classmethod
    def _gen_union_attr(cls, union_field, offset):
        if not isinstance(union_field, Union):
            raise ValueError("field should be an Union instance")
        for name, field in union_field.field_list:
            cls.gen_field(name, field, offset)


class PinnedSelf(PinnedStruct):
    """Special Marker class for reference to current class in a Ptr or Array
    (mostly Array of Ptr).

    Example:
        class ListNode(PinnedStruct):
            fields = [
                ("next", Ptr("<I", PinnedSelf)),
                ("data", Ptr("<I", PinnedVoid)),
            ]
    """
    pass


class PinnedVoid(PinnedType):
    """Placeholder for e.g. Ptr to an undetermined type. Useful mostly when
    casted to another type. Allows to implement C's "void*" pattern.
    """
    def __repr__(self):
        return self.__class__.__name__


# This does not use _MetaPinnedStruct features, impl is custom for strings,
# because they are unsized. The only memory field is self.value.
class PinnedStr(PinnedType):
    """Implements a string representation in memory.

    The @encoding is passed to the constructor, and is currently either null
    terminated "ansi" (latin1) or (double) null terminated "utf16". Be aware
    that the utf16 implementation is a bit buggy...

    The string value can be got or set (with python str/unicode) through the
    self.value attribute. String encoding/decoding is handled by the class.

    This type is dynamically sized only (get_size is implemented, not sizeof).
    """
    def __init__(self, vm, addr, encoding="ansi"):
        # TODO: encoding as lambda
        if encoding not in ["ansi", "utf16"]:
            raise NotImplementedError("Only 'ansi' and 'utf16' are implemented")
        super(PinnedStr, self).__init__(vm, addr)
        self._enc = encoding

    @property
    def value(self):
        """Set the string value in memory"""
        if self._enc == "ansi":
            get_str = get_str_ansi
        elif self._enc == "utf16":
            get_str = get_str_utf16
        else:
            raise NotImplementedError("Only 'ansi' and 'utf16' are implemented")
        return get_str(self._vm, self.get_addr())

    @value.setter
    def value(self, s):
        """Get the string value from memory"""
        if self._enc == "ansi":
            set_str = set_str_ansi
        elif self._enc == "utf16":
            set_str = set_str_utf16
        else:
            raise NotImplementedError("Only 'ansi' and 'utf16' are implemented")
        set_str(self._vm, self.get_addr(), s)

    def get_size(self):
        """This get_size implementation is quite unsafe: it reads the string
        underneath to determine the size, it may therefore read a lot of memory
        and provoke mem faults (analogous to strlen).
        """
        val = self.value
        if self._enc == "ansi":
            return len(val) + 1
        elif self._enc == "utf16":
            # FIXME: real encoding...
            return len(val) * 2 + 2
        else:
            raise NotImplementedError("Only 'ansi' and 'utf16' are implemented")

    def raw(self):
        raw = self._vm.get_mem(self.get_addr(), self.get_size())
        return raw

    def __repr__(self):
        return "%r(%s): %r" % (self.__class__, self._enc, self.value)


class PinnedArray(PinnedType):
    """An unsized array of type @field_type (a Type subclass instance).
    This class has no static or dynamic size.

    It can be indexed for setting and getting elements, example:

        array = PinnedArray(vm, addr, Num("I"))
        array[2] = 5
        array[4:8] = [0, 1, 2, 3]
        print array[20]

    If the @field_type is a Ptr, deref_get(index) and deref_set(index) can be
    used to dereference a field at a given index in the array.

    mem_array_type can be used to generate a type that includes the field_type.
    Such a generated type can be instanciated with only vm and addr, as are
    other PinnedTypes.
    """
    _field_type = None

    def __init__(self, vm, addr=None, field_type=None):
        if self._field_type is None:
            self._field_type = field_type
        if self._field_type is None:
            raise NotImplementedError(
                "Provide field_type to instanciate this class, "
                "or generate a subclass with mem_array_type.")
        super(PinnedArray, self).__init__(vm, addr)

    @property
    def field_type(self):
        """Return the Type subclass instance that represents the type of
        this PinnedArray items.
        """
        return self._field_type

    def _normalize_idx(self, idx):
        # Noop for this type
        return idx

    def _normalize_slice(self, slice_):
        start = slice_.start if slice_.start is not None else 0
        stop = slice_.stop if slice_.stop is not None else self.get_size()
        step = slice_.step if slice_.step is not None else 1
        return slice(start, stop, step)

    def _check_bounds(self, idx):
        idx = self._normalize_idx(idx)
        if not isinstance(idx, (int, long)):
            raise ValueError("index must be an int or a long")
        if idx < 0:
            raise IndexError("Index %s out of bounds" % idx)

    def index2addr(self, idx):
        """Return the address corresponding to a given @index in this PinnedArray.
        """
        self._check_bounds(idx)
        addr = self.get_addr() + idx * self._field_type.size()
        return addr

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            res = []
            idx = self._normalize_slice(idx)
            for i in xrange(idx.start, idx.stop, idx.step):
                res.append(self._field_type.get(self._vm, self.index2addr(i)))
            return res
        else:
            return self._field_type.get(self._vm, self.index2addr(idx))

    def deref_get(self, idx):
        """If self.field_type is a Ptr, return the PinnedType self[idx] points
        to.
        """
        return self._field_type.deref_get(self._vm, self[idx])

    def __setitem__(self, idx, item):
        if isinstance(idx, slice):
            idx = self._normalize_slice(idx)
            if len(item) != len(xrange(idx.start, idx.stop, idx.step)):
                raise ValueError("Mismatched lengths in slice assignment")
            # TODO: izip
            for i, val in zip(xrange(idx.start, idx.stop, idx.step), item):
                self._field_type.set(self._vm, self.index2addr(i), val)
        else:
            self._field_type.set(self._vm, self.index2addr(idx), item)

    def deref_set(self, idx, item):
        """If self.field_type is a Ptr, set the PinnedType self[idx] points
        to with @item.
        """
        self._field_type.deref_set(self._vm, self[idx], item)

    # just a shorthand
    def as_mem_str(self, encoding="ansi"):
        return self.cast(PinnedStr, encoding)

    @classmethod
    def sizeof(cls):
        raise ValueError("%s is unsized, it has no static size (sizeof). "
                         "Use PinnedSizedArray instead." % cls)

    def raw(self):
        raise ValueError("%s is unsized, which prevents from getting its full "
                         "raw representation. Use PinnedSizedArray instead." %
                         self.__class__)

    def __repr__(self):
        return "[%r, ...] [%r]" % (self[0], self._field_type)


class PinnedSizedArray(PinnedArray):
    """A fixed size PinnedArray. Its additional arg represents the @array_len (in
    number of elements) of this array.

    This type is dynamically sized. Use mem_sized_array_type to generate a
    fixed @field_type and @array_len array which has a static size.
    """
    _array_len = None

    def __init__(self, vm, addr=None, field_type=None, array_len=None):
        # Set the length before anything else to allow get_size() to work for
        # allocation
        if self._array_len is None:
            self._array_len = array_len
        super(PinnedSizedArray, self).__init__(vm, addr, field_type)
        if self._array_len is None or self._field_type is None:
            raise NotImplementedError(
                "Provide field_type and array_len to instanciate this class, "
                "or generate a subclass with mem_sized_array_type.")

    @property
    def array_len(self):
        """The length, in number of elements, of this array."""
        return self._array_len

    def sizeof(cls):
        raise ValueError("PinnedSizedArray is not statically sized. Use "
                         "mem_sized_array_type to generate a type that is.")

    def get_size(self):
        return self._array_len * self._field_type.size()

    def _normalize_idx(self, idx):
        if idx < 0:
            return self.get_size() - idx
        return idx

    def _check_bounds(self, idx):
        if not isinstance(idx, int) and not isinstance(idx, long):
            raise ValueError("index must be an int or a long")
        if idx < 0 or idx >= self.get_size():
            raise IndexError("Index %s out of bounds" % idx)

    def __iter__(self):
        for i in xrange(self._array_len):
            yield self[i]

    def raw(self):
        return self._vm.get_mem(self.get_addr(), self.get_size())

    def __repr__(self):
        item_reprs = [repr(item) for item in self]
        if self._array_len > 0 and '\n' in item_reprs[0]:
            items = '\n' + indent(',\n'.join(item_reprs), 2) + '\n'
        else:
            items = ', '.join(item_reprs)
        return "[%s] [%r; %s]" % (items, self._field_type, self._array_len)

    def __eq__(self, other):
        # Special implementation to handle dynamic subclasses
        return isinstance(other, PinnedSizedArray) and \
                self._field_type == other._field_type and \
                self._array_len == other._array_len and \
                str(self) == str(other)


def mem_array_type(field_type):
    """Generate a PinnedArray subclass that has a fixed @field_type. It allows to
    instanciate this class with only vm and addr argument, as are standard
    PinnedTypes.
    """
    cache_key = (field_type, None)
    if cache_key in DYN_MEM_STRUCT_CACHE:
        return DYN_MEM_STRUCT_CACHE[cache_key]

    array_type = type('PinnedArray_%r' % (field_type,),
                      (PinnedArray,),
                      {'_field_type': field_type})
    DYN_MEM_STRUCT_CACHE[cache_key] = array_type
    return array_type


def mem_sized_array_type(field_type, array_len):
    """Generate a PinnedSizedArray subclass that has a fixed @field_type and a
    fixed @array_len. This allows to instanciate the returned type with only
    the vm and addr arguments, as are standard PinnedTypes.
    """
    cache_key = (field_type, array_len)
    if cache_key in DYN_MEM_STRUCT_CACHE:
        return DYN_MEM_STRUCT_CACHE[cache_key]

    @classmethod
    def sizeof(cls):
        return cls._field_type.size() * cls._array_len

    array_type = type('PinnedSizedArray_%r_%s' % (field_type, array_len),
                      (PinnedSizedArray,),
                      {'_array_len': array_len,
                       '_field_type': field_type,
                       'sizeof': sizeof})
    DYN_MEM_STRUCT_CACHE[cache_key] = array_type
    return array_type

