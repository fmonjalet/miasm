#!/usr/bin/env python
"""This script is just a short example of common usages for miasm2.analysis.mem.
For a more complete view of what is possible, tests/analysis/mem.py covers
most of the module possibilities, and the module doc gives useful information
as well.
"""

from miasm2.analysis.machine import Machine
from miasm2.analysis.mem import MemStruct, MemSelf, MemVoid, MemStr,\
                                MemSizedArray, Ptr, Num, Array, set_allocator
from miasm2.os_dep.common import heap

# Instanciate a heap
my_heap = heap()
# And set it as the default memory allocator, to avoid manual allocation and
# explicit address passing to the MemStruct constructor
set_allocator(my_heap.vm_alloc)

# Let's reimplement a simple C generic linked list mapped on a VmMngr!

# All the structures and methods will use the python objects but all the data
# is in fact stored in the VmMngr

class ListNode(MemStruct):
    fields = [
        # The "<I" is the struct-like format of the pointer in memory, in this
        # case a Little Endian 32 bits unsigned int
        # One way to handle reference to ListNode in ListNode is to use the
        # special marker MemSelf.
        # You could also set or modify ListNode.fields after the class
        # declaration and call ListNode.gen_fields()
        ("next", Ptr("<I", MemSelf)),
        # Ptr(_, MemVoid) is analogous to void*, MemVoid is just an empty
        # MemStruct type
        ("data", Ptr("<I", MemVoid)),
    ]

    def get_next(self):
        if self.next == 0:
            return None
        return self.deref_next

    def get_data(self, data_type=None):
        if data_type is not None:
            return self.deref_data.cast(data_type)
        else:
            return self.deref_data


class LinkedList(MemStruct):
    fields = [
        ("head", Ptr("<I", ListNode)),
        ("tail", Ptr("<I", ListNode)),
        # Num can take any one-field struct-like format, including floats and
        # doubles
        ("size", Num("<I")),
    ]

    def __init__(self, vm, *args, **kwargs):
        super(LinkedList, self).__init__(vm, *args, **kwargs)
        self.memset()

    def get_head(self):
        """Returns the head ListNode instance"""
        if self.head == 0:
            return None
        return self.deref_head

    def get_tail(self):
        if self.tail == 0:
            return None
        return self.deref_tail

    def push(self, data):
        # Allocate a new node
        node = ListNode(self._vm)

        # Set the data pointer
        node.data = data.get_addr()

        # re-link
        if self.head != 0:
            # get the head ListNode
            head = self.get_head()
            node.next = head.get_addr()

        # pointer to head assigned to the new node address
        self.head = node.get_addr()

        # Do not forget the tail :)
        if self.tail == 0:
            self.tail = node.get_addr()

        self.size += 1

    def pop(self, data_type=None):
        # Nothing to pop
        if self.head == 0:
            return None

        node = self.get_head()
        self.head = node.next

        # empty
        if self.head == 0:
            self.tail = 0

        self.size -= 1

        return node.get_data(data_type)

    def empty(self):
        return self.head == 0

    def __iter__(self):
        if not self.empty():
            cur = self.get_head()
            while cur is not None:
                yield cur.deref_data
                cur = cur.get_next()


# Some data types to put in the LinkedList and play with:

class DataArray(MemStruct):
    fields = [
        ("val1", Num("B")),
        ("val2", Num("B")),
        # Ptr can also be instanciated with a MemField as an argument, a special
        # MemStruct containing only one field named "value" will be created, so
        # that Ptr can point to a MemStruct instance. Here,
        # data_array.deref_array.value will allow to access an Array
        ("arrayptr", Ptr("<I", MemSizedArray, Num("B"), 16)),
        # Array of 10 uint8
        ("array", Array(Num("B"), 16)),
    ]

class DataStr(MemStruct):
    fields = [
        ("valshort", Num("H")),
        # Pointer to an utf16 null terminated string
        ("data", Ptr("<I", MemStr, "utf16")),
    ]


print "This script demonstrates a LinkedList implementation using the mem "
print "module in the first part, and how to play with some casts in the second."
print

# A random jitter
# You can also use miasm2.jitter.VmMngr.Vm(), but it does not happen in real
# life scripts, so here is the usual way:
jitter = Machine("x86_32").jitter("python")
vm = jitter.vm

# Auto-allocated by my_heap. If you allocate memory at `addr`,
# `link = LinkedList(vm, addr)` will use this allocation.
link = LinkedList(vm)

# Push three uninitialized structures
link.push(DataArray(vm))
link.push(DataArray(vm))
link.push(DataArray(vm))

# Size has been updated
assert link.size == 3
# If you get it directly from the VM, it is updated as well
raw_size = vm.get_mem(link.get_addr("size"), link.get_field_type("size").size())
assert raw_size == '\x03\x00\x00\x00'

print "The linked list just built:"
print repr(link), '\n'

print "Its uninitialized data elements:"
for data in link:
    # __iter__ returns MemVoids here, just cast them to the real data type
    real_data = data.cast(DataArray)
    print repr(real_data)
print

# Now let's play with one data
data = link.pop(DataArray)
assert link.size == 2
# Make the Array Ptr point to the data's array field
data.arrayptr = data.get_addr("array")
# Now the pointer dereference is equal to the array field's value
assert data.deref_arrayptr == data.array

# Let's say that it is a DataStr:
datastr = data.cast(DataStr)

print "First element casted to DataStr:"
print repr(datastr)
print

# data and datastr really share the same memory:
# Set these fields for later
data.val1 = 0x34
data.val2 = 0x12

assert datastr.valshort == 0x1234
datastr.valshort = 0x1122
assert data.val1 == 0x22 and data.val2 == 0x11

# Let's play with strings
memstr = datastr.deref_data
# Note that memstr is MemStr(..., "utf16")
memstr.value = 'Miams'

print "Cast data.array to MemStr and set the string value:"
print repr(memstr)
print

# If you followed, memstr and data.array point to the same object, so:
raw_miams = '\x00'.join('Miams') + '\x00'*3
raw_miams_array = [ord(c) for c in raw_miams]
assert list(data.array)[:len(raw_miams_array)] == raw_miams_array
assert data.array.cast(MemStr, "utf16") == memstr
# Default is "ansi"
assert data.array.cast(MemStr) != memstr
assert data.array.cast(MemStr, "utf16").value == memstr.value

print "See that the original array has been modified:"
print repr(data)
print

print "See test/analysis/mem.py and the miasm2.analysis.mem module doc for "
print "more information."

