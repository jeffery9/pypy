import sys
from pypy.rpython.lltypesystem import lltype, llmemory, rffi, rclass, rstr
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rlib.objectmodel import we_are_translated, specialize
from pypy.rlib import runicode
from pypy.jit.metainterp.history import AbstractDescr, INT
from pypy.jit.metainterp.history import BoxInt, BoxPtr
from pypy.jit.backend import model
from pypy.jit.backend.llvm import llvm_rffi
from pypy.jit.metainterp import history
from pypy.jit.metainterp.resoperation import rop, ResOperation
from pypy.jit.backend.x86 import symbolic     # xxx

history.TreeLoop._llvm_compiled_index = -1


class LLVMCPU(model.AbstractCPU):
    is_oo = False
    RAW_VALUE = rffi.CFixedArray(rffi.ULONGLONG, 1)
    SIGNED_VALUE = rffi.CFixedArray(lltype.Signed, 1)
    POINTER_VALUE = rffi.CFixedArray(llmemory.GCREF, 1)

    SIZE_GCPTR   = 0
    SIZE_INT     = 1
    SIZE_CHAR    = 2
    SIZE_UNICHAR = 3

    def __init__(self, rtyper, stats=None, translate_support_code=False,
                 annmixlevel=None, gcdescr=None):
        self.rtyper = rtyper
        self.translate_support_code = translate_support_code
        self.compiled_functions = []
        self.fail_ops = []
        self.in_out_args = []
        if translate_support_code:
            get_size = llmemory.sizeof
        else:
            get_size = rffi.sizeof
        self._arraydescrs = [
            ArrayDescr(get_size(llmemory.GCREF), self.SIZE_GCPTR),    # 0
            ArrayDescr(get_size(lltype.Signed),  self.SIZE_INT),      # 1
            ArrayDescr(get_size(lltype.Char),    self.SIZE_CHAR),     # 2
            ArrayDescr(get_size(lltype.UniChar), self.SIZE_UNICHAR),  # 3
            ]
        self._descr_caches = {}
        self.fielddescr_vtable = self.fielddescrof(rclass.OBJECT, 'typeptr')
        if sys.maxint == 2147483647:
            self.size_of_int = 4
        else:
            self.size_of_int = 8
        if runicode.MAXUNICODE > 0xffff:
            self.size_of_unicode = 4
        else:
            self.size_of_unicode = 2
        basesize, _, ofs_length = symbolic.get_array_token(
            lltype.GcArray(lltype.Signed), self.translate_support_code)
        self.array_index_array = basesize
        self.array_index_length = ofs_length
        basesize, _, ofs_length = symbolic.get_array_token(
            rstr.STR, self.translate_support_code)
        self.string_index_array = basesize
        self.string_index_length = ofs_length
        basesize, _, ofs_length = symbolic.get_array_token(
            rstr.UNICODE, self.translate_support_code)
        self.unicode_index_array = basesize
        self.unicode_index_length = ofs_length
        self.vtable_descr = self.fielddescrof(rclass.OBJECT, 'typeptr')
        self._ovf_error_instance = self._get_prebuilt_error(OverflowError)
        self._zer_error_instance = self._get_prebuilt_error(ZeroDivisionError)
        #
        # temporary (Boehm only)
        from pypy.translator.tool.cbuild import ExternalCompilationInfo
        compilation_info = ExternalCompilationInfo(libraries=['gc'])
        self.malloc_fn_ptr = rffi.llexternal("GC_malloc",
                                             [rffi.SIZE_T],
                                             llmemory.GCREF,
                                             compilation_info=compilation_info,
                                             sandboxsafe=True,
                                             _nowrapper=True)
        assert rffi.sizeof(rffi.SIZE_T) == self.size_of_int

    def setup_once(self):
        if not we_are_translated():
            llvm_rffi.teardown_now()
        llvm_rffi.LLVM_SetFlags()
        self.module = llvm_rffi.LLVMModuleCreateWithName("pypyjit")
        if self.size_of_int == 4:
            self.ty_int = llvm_rffi.LLVMInt32Type()
        else:
            self.ty_int = llvm_rffi.LLVMInt64Type()
        if self.size_of_unicode == 2:
            self.ty_unichar = llvm_rffi.LLVMInt16Type()
        else:
            self.ty_unichar = llvm_rffi.LLVMInt32Type()
        self.ty_void = llvm_rffi.LLVMVoidType()
        self.ty_bit = llvm_rffi.LLVMInt1Type()
        self.ty_char = llvm_rffi.LLVMInt8Type()
        self.ty_char_ptr = llvm_rffi.LLVMPointerType(self.ty_char, 0)
        self.ty_char_ptr_ptr = llvm_rffi.LLVMPointerType(self.ty_char_ptr, 0)
        self.ty_int_ptr = llvm_rffi.LLVMPointerType(self.ty_int, 0)
        self.ty_int_ptr_ptr = llvm_rffi.LLVMPointerType(self.ty_int_ptr, 0)
        self.ty_unichar_ptr = llvm_rffi.LLVMPointerType(self.ty_unichar, 0)
        self.const_zero = self._make_const_int(0)
        self.const_one  = self._make_const_int(1)
        self.const_null_charptr = self._make_const(0, self.ty_char_ptr)
        #
        from pypy.jit.backend.llvm.compile import LLVMJITCompiler
        self.types_by_index = [self.ty_char_ptr,     # SIZE_GCPTR
                               self.ty_int,          # SIZE_INT
                               self.ty_char,         # SIZE_CHAR
                               self.ty_unichar]      # SIZE_UNICHAR
        self.types_ptr_by_index = [self.ty_char_ptr_ptr,   # SIZE_GCPTR
                                   self.ty_int_ptr,        # SIZE_INT
                                   self.ty_char_ptr,       # SIZE_CHAR
                                   self.ty_unichar_ptr]    # SIZE_UNICHAR
        self.getarg_by_index = [LLVMJITCompiler.getptrarg,     # SIZE_GCPTR
                                LLVMJITCompiler.getintarg,     # SIZE_INT
                                LLVMJITCompiler.getchararg,    # SIZE_CHAR
                                LLVMJITCompiler.getunichararg] # SIZE_UNICHAR
        for i in range(len(self.types_by_index)):
            arraydescr = self._arraydescrs[i]
            (arraydescr.ty_array_ptr,
             self.const_array_index_length,
             self.const_array_index_array) = \
                    self._build_ty_array_ptr(self.array_index_array,
                                             self.types_by_index[i],
                                             self.array_index_length)
        (self.ty_string_ptr,
         self.const_string_index_length,
         self.const_string_index_array) = \
                 self._build_ty_array_ptr(self.string_index_array,
                                          self.ty_char,
                                          self.string_index_length)
        (self.ty_unicode_ptr,
         self.const_unicode_index_length,
         self.const_unicode_index_array) = \
                 self._build_ty_array_ptr(self.unicode_index_array,
                                          self.ty_unichar,
                                          self.unicode_index_length)
        #
        arglist = lltype.malloc(rffi.CArray(llvm_rffi.LLVMTypeRef), 0,
                                flavor='raw')
        self.ty_func = llvm_rffi.LLVMFunctionType(self.ty_int, arglist, 0,
                                                  False)
        lltype.free(arglist, flavor='raw')
        #
        self.f_add_ovf = llvm_rffi.LLVM_Intrinsic_add_ovf(self.module,
                                                          self.ty_int)
        self.f_sub_ovf = llvm_rffi.LLVM_Intrinsic_sub_ovf(self.module,
                                                          self.ty_int)
        self.f_mul_ovf = llvm_rffi.LLVM_Intrinsic_mul_ovf(self.module,
                                                          self.ty_int)
        if we_are_translated():
            addr = llop.get_exception_addr(llmemory.Address)
            self.exc_type = rffi.cast(rffi.CArrayPtr(lltype.Signed), addr)
            addr = llop.get_exc_value_addr(llmemory.Address)
            self.exc_value = rffi.cast(rffi.CArrayPtr(llmemory.GCREF), addr)
        else:
            self.exc_type = lltype.malloc(rffi.CArray(lltype.Signed), 1,
                                          zero=True, flavor='raw')
            self.exc_value = lltype.malloc(rffi.CArray(llmemory.GCREF), 1,
                                           zero=True, flavor='raw')
        self.backup_exc_type = lltype.malloc(rffi.CArray(lltype.Signed), 1,
                                             zero=True, flavor='raw')
        self.backup_exc_value = lltype.malloc(rffi.CArray(llmemory.GCREF), 1,
                                              zero=True, flavor='raw')
        self.const_exc_type = self._make_const(self.exc_type,
                                               self.ty_char_ptr_ptr)
        self.const_exc_value = self._make_const(self.exc_value,
                                                self.ty_char_ptr_ptr)
        self.const_backup_exc_type = self._make_const(self.backup_exc_type,
                                                      self.ty_char_ptr_ptr)
        self.const_backup_exc_value = self._make_const(self.backup_exc_value,
                                                       self.ty_char_ptr_ptr)
        #
        self._setup_prebuilt_error('ovf')
        self._setup_prebuilt_error('zer')
        #
        # temporary (Boehm only)
        param_types = lltype.malloc(rffi.CArray(llvm_rffi.LLVMTypeRef), 1,
                                    flavor='raw')
        param_types[0] = self.ty_int
        self.ty_malloc_fn = llvm_rffi.LLVMPointerType(
            llvm_rffi.LLVMFunctionType(self.ty_char_ptr, param_types, 1, 0),
            0)
        lltype.free(param_types, flavor='raw')
        #
        self.ee = llvm_rffi.LLVM_EE_Create(self.module)
        if not we_are_translated():
            llvm_rffi.set_teardown_function(self._teardown)

    def _teardown(self):
        llvm_rffi.LLVMDisposeExecutionEngine(self.ee)

    def _get_prebuilt_error(self, Class):
        "NOT_RPYTHON"
        if self.rtyper is not None:   # normal case
            bk = self.rtyper.annotator.bookkeeper
            clsdef = bk.getuniqueclassdef(Class)
            ll_inst = self.rtyper.exceptiondata.get_standard_ll_exc_instance(
                self.rtyper, clsdef)
        else:
            # for tests, a random emulated ll_inst will do
            ll_inst = lltype.malloc(rclass.OBJECT)
            ll_inst.typeptr = lltype.malloc(rclass.OBJECT_VTABLE,
                                            immortal=True)
        return ll_inst

    @specialize.arg(1)
    def _setup_prebuilt_error(self, prefix):
        ll_inst = getattr(self, '_' + prefix + '_error_instance')
        setattr(self, '_' + prefix + '_error_type',
                rffi.cast(lltype.Signed, ll_inst.typeptr))
        setattr(self, '_' + prefix + '_error_value',
                lltype.cast_opaque_ptr(llmemory.GCREF, ll_inst))
        setattr(self, 'const_' + prefix + '_error_type',
                self._make_const(ll_inst.typeptr, self.ty_char_ptr))
        setattr(self, 'const_' + prefix + '_error_value',
                self._make_const(ll_inst, self.ty_char_ptr))

    def _build_ty_array_ptr(self, basesize, ty_item, ofs_length):
        pad1 = ofs_length
        pad2 = basesize - ofs_length - self.size_of_int
        assert pad1 >= 0 and pad2 >= 0
        const_index_length = self._make_const_int(pad1)
        const_index_array = self._make_const_int(pad1 + 1 + pad2)
        # build the type "struct{pad1.., length, pad2.., array{type}}"
        typeslist = lltype.malloc(rffi.CArray(llvm_rffi.LLVMTypeRef),
                                  pad1+pad2+2, flavor='raw')
        # add the first padding
        for n in range(pad1):
            typeslist[n] = self.ty_char
        # add the length field
        typeslist[pad1] = self.ty_int
        # add the second padding
        for n in range(pad1+1, pad1+1+pad2):
            typeslist[n] = self.ty_char
        # add the array field
        typeslist[pad1+1+pad2] = llvm_rffi.LLVMArrayType(ty_item, 0)
        # done
        ty_array = llvm_rffi.LLVMStructType(typeslist,
                                            pad1+pad2+2,
                                            1)
        lltype.free(typeslist, flavor='raw')
        ty_array_ptr = llvm_rffi.LLVMPointerType(ty_array, 0)
        return (ty_array_ptr, const_index_length, const_index_array)

    # ------------------------------
    # Compilation

    def compile_operations(self, loop, _guard_op=None):
        from pypy.jit.backend.llvm.compile import LLVMJITCompiler
        compiler = LLVMJITCompiler(self, loop)
        compiler.compile()
        llvm_rffi.LLVMDumpModule(self.module)   # xxx for debugging

    def _ensure_in_args(self, count):
        while len(self.in_out_args) < count:
            self.in_out_args.append(lltype.malloc(self.RAW_VALUE, flavor='raw'))

    _ensure_out_args = _ensure_in_args

    def _make_const_int(self, value):
        return llvm_rffi.LLVMConstInt(self.ty_int, value, True)

    def _make_const_char(self, value):
        assert (value & ~255) == 0, "value is not in range(256)"
        return llvm_rffi.LLVMConstInt(self.ty_char, value, True)

    def _make_const_unichar(self, value):
        #xxx assert something about 'value'
        return llvm_rffi.LLVMConstInt(self.ty_unichar, value, True)

    def _make_const_bit(self, value):
        assert (value & ~1) == 0, "value is not 0 or 1"
        return llvm_rffi.LLVMConstInt(self.ty_bit, value, True)

    @specialize.arglltype(1)
    def _make_const(self, value, ty_result):
        value_as_signed = rffi.cast(lltype.Signed, value)
        llvmconstint = self._make_const_int(value_as_signed)
        llvmconstptr = llvm_rffi.LLVMConstIntToPtr(llvmconstint, ty_result)
        return llvmconstptr

    def _lltype2llvmtype(self, TYPE):
        if TYPE is lltype.Void:
            return self.ty_void
        elif isinstance(TYPE, lltype.Ptr) and TYPE.TO._gckind == 'gc':
            return self.ty_char_ptr
        else:
            return self.ty_int

    def _get_var_type(self, v):
        if v.type == INT:
            return self.ty_int
        else:
            return self.ty_char_ptr

    def _get_pointer_type(self, v):
        if v.type == INT:
            return self.ty_int_ptr
        else:
            return self.ty_char_ptr_ptr

    # ------------------------------
    # Execution

    def set_future_value_int(self, index, intvalue):
        p = rffi.cast(lltype.Ptr(self.SIGNED_VALUE), self.in_out_args[index])
        p[0] = intvalue

    def set_future_value_ptr(self, index, ptrvalue):
        p = rffi.cast(lltype.Ptr(self.POINTER_VALUE), self.in_out_args[index])
        p[0] = ptrvalue

    def execute_operations(self, loop):
        index = loop._llvm_compiled_index
        assert index >= 0
        while True:
            func_ptr = self.compiled_functions[index]
            print 'execute_operations: %d (at 0x%x)' % (
                index,  rffi.cast(lltype.Signed, func_ptr))
            index = func_ptr()
            print '\t--->', index
            if index < 0:
                break
        return self.fail_ops[~index]

    def get_latest_value_int(self, index):
        p = rffi.cast(lltype.Ptr(self.SIGNED_VALUE), self.in_out_args[index])
        return p[0]

    def get_latest_value_ptr(self, index):
        p = rffi.cast(lltype.Ptr(self.POINTER_VALUE), self.in_out_args[index])
        return p[0]

    def get_exception(self):
        return self.backup_exc_type[0]

    def get_exc_value(self):
        return self.backup_exc_value[0]

    def clear_exception(self):
        self.backup_exc_type[0] = 0
        self.backup_exc_value[0] = lltype.nullptr(llmemory.GCREF.TO)

    def set_overflow_error(self):
        self.backup_exc_type[0] = self._ovf_error_type
        self.backup_exc_value[0] = self._ovf_error_value

    def set_zero_division_error(self):
        self.backup_exc_type[0] = self._zer_error_type
        self.backup_exc_value[0] = self._zer_error_value

    @staticmethod
    def cast_adr_to_int(x):
        return rffi.cast(lltype.Signed, x)

    @staticmethod
    def cast_int_to_adr(x):
        if we_are_translated():
            return rffi.cast(llmemory.Address, x)
        else:
            # indirect casting because the above doesn't work with ll2ctypes
            return llmemory.cast_ptr_to_adr(rffi.cast(llmemory.GCREF, x))

    def _get_size_index(self, TYPE):
        if isinstance(TYPE, lltype.Ptr):
            if TYPE.TO._gckind == 'gc':
                return self.SIZE_GCPTR
            else:
                return self.SIZE_INT
        else:
            if TYPE == lltype.Signed:
                return self.SIZE_INT
            elif TYPE == lltype.Char:
                return self.SIZE_CHAR
            elif TYPE == lltype.UniChar:
                return self.SIZE_UNICHAR
            else:
                raise BadSizeError(S, fieldname, size)

    def sizeof(self, S):
        try:
            return self._descr_caches['size', S]
        except KeyError:
            pass
        descr = SizeDescr(symbolic.get_size(S, self.translate_support_code))
        self._descr_caches['size', S] = descr
        return descr

    def fielddescrof(self, S, fieldname):
        try:
            return self._descr_caches['field', S, fieldname]
        except KeyError:
            pass
        ofs, _ = symbolic.get_field_token(S, fieldname,
                                          self.translate_support_code)
        size_index = self._get_size_index(getattr(S, fieldname))
        descr = FieldDescr(ofs, size_index)
        self._descr_caches['field', S, fieldname] = descr
        return descr

    def arraydescrof(self, A):
        basesize, _, ofs_length = symbolic.get_array_token(A,
                                               self.translate_support_code)
        assert self.array_index_array == basesize
        assert self.array_index_length == ofs_length
        itemsize_index = self._get_size_index(A.OF)
        return self._arraydescrs[itemsize_index]

    def calldescrof(self, FUNC, ARGS, RESULT):
        try:
            return self._descr_caches['call', ARGS, RESULT]
        except KeyError:
            pass
        #
        param_types = lltype.malloc(rffi.CArray(llvm_rffi.LLVMTypeRef),
                                    len(ARGS), flavor='raw')
        for i in range(len(ARGS)):
            param_types[i] = self._lltype2llvmtype(ARGS[i])
        ty_func = llvm_rffi.LLVMFunctionType(self._lltype2llvmtype(RESULT),
                                             param_types, len(ARGS), 0)
        lltype.free(param_types, flavor='raw')
        ty_funcptr = llvm_rffi.LLVMPointerType(ty_func, 0)
        #
        if RESULT is lltype.Void:
            result_mask = 0
        elif isinstance(RESULT, lltype.Ptr) and RESULT.TO._gckind == 'gc':
            result_mask = -2
        else:
            result_size = symbolic.get_size(RESULT,
                                            self.translate_support_code)
            if result_size < self.size_of_int:
                result_mask = (1 << (result_size*8)) - 1
            else:
                result_mask = -1
        descr = CallDescr(ty_funcptr, result_mask)
        self._descr_caches['call', ARGS, RESULT] = descr
        return descr

    # ------------------------------
    # do_xxx methods

    def do_arraylen_gc(self, args, arraydescr):
        array = args[0].getptr_base()
        p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.Signed)), array)
        res = len(p)
        return BoxInt(res)

    def do_strlen(self, args, descr=None):
        s = args[0].getptr_base()
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.STR), s)
        res = len(p.chars)
        return BoxInt(res)

    def do_strgetitem(self, args, descr=None):
        s = args[0].getptr_base()
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.STR), s)
        res = ord(p.chars[args[1].getint()])
        return BoxInt(res)

    def do_unicodelen(self, args, descr=None):
        s = args[0].getptr_base()
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.UNICODE), s)
        res = len(p.chars)
        return BoxInt(res)

    def do_unicodegetitem(self, args, descr=None):
        s = args[0].getptr_base()
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.UNICODE), s)
        res = ord(p.chars[args[1].getint()])
        return BoxInt(res)

    def do_getarrayitem_gc(self, args, arraydescr):
        array = args[0].getptr_base()
        index = args[1].getint()
        assert isinstance(arraydescr, ArrayDescr)
        itemsize_index = arraydescr.itemsize_index
        if itemsize_index == self.SIZE_GCPTR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(llmemory.GCREF)), array)
            res = p[index]
            return BoxPtr(res)
        elif itemsize_index == self.SIZE_INT:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.Signed)), array)
            res = p[index]
        elif itemsize_index == self.SIZE_CHAR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.Char)), array)
            res = ord(p[index])
        elif itemsize_index == self.SIZE_UNICHAR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.UniChar)), array)
            res = ord(p[index])
        else:
            raise BadSizeError
        return BoxInt(res)

    @specialize.argtype(1)
    def _do_getfield(self, struct, fielddescr):
        assert isinstance(fielddescr, FieldDescr)
        size_index = fielddescr.size_index
        if size_index == self.SIZE_GCPTR:
            p = rffi.cast(rffi.CArrayPtr(llmemory.GCREF), struct)
            res = p[fielddescr.offset / rffi.sizeof(llmemory.GCREF)]
            return BoxPtr(res)
        elif size_index == self.SIZE_INT:
            p = rffi.cast(rffi.CArrayPtr(lltype.Signed), struct)
            res = p[fielddescr.offset / rffi.sizeof(lltype.Signed)]
        elif size_index == self.SIZE_CHAR:
            p = rffi.cast(rffi.CArrayPtr(lltype.Char), struct)
            res = ord(p[fielddescr.offset / rffi.sizeof(lltype.Char)])
        elif size_index == self.SIZE_UNICHAR:
            p = rffi.cast(rffi.CArrayPtr(lltype.UniChar), struct)
            res = ord(p[fielddescr.offset / rffi.sizeof(lltype.UniChar)])
        else:
            raise BadSizeError
        return BoxInt(res)

    def do_getfield_gc(self, args, fielddescr):
        struct = args[0].getptr_base()
        return self._do_getfield(struct, fielddescr)

    def do_getfield_raw(self, args, fielddescr):
        struct = args[0].getaddr(self)
        return self._do_getfield(struct, fielddescr)

    def do_new(self, args, sizedescr):
        assert isinstance(sizedescr, SizeDescr)
        res = self.malloc_fn_ptr(rffi.cast(rffi.SIZE_T, sizedescr.size))
        return BoxPtr(res)

    def do_new_with_vtable(self, args, sizedescr):
        assert isinstance(sizedescr, SizeDescr)
        res = self.malloc_fn_ptr(rffi.cast(rffi.SIZE_T, sizedescr.size))
        self._do_setfield(res, args[0], self.vtable_descr)
        return BoxPtr(res)

    def _allocate_new_array(self, args, item_size, index_array, index_length):
        length = args[0].getint()
        #try:
        size = index_array + length * item_size
        #except OverflowError:
        #    ...
        res = self.malloc_fn_ptr(rffi.cast(rffi.SIZE_T, size))
        p = rffi.cast(rffi.CArrayPtr(lltype.Signed), res)
        p[index_length / rffi.sizeof(lltype.Signed)] = length
        return BoxPtr(res)

    def do_new_array(self, args, arraydescr):
        assert isinstance(arraydescr, ArrayDescr)
        return self._allocate_new_array(args, arraydescr.itemsize,
                                        self.array_index_array,
                                        self.array_index_length)

    def do_setarrayitem_gc(self, args, arraydescr):
        array = args[0].getptr_base()
        index = args[1].getint()
        assert isinstance(arraydescr, ArrayDescr)
        itemsize_index = arraydescr.itemsize_index
        if itemsize_index == self.SIZE_GCPTR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(llmemory.GCREF)), array)
            res = args[2].getptr_base()
            p[index] = res
        elif itemsize_index == self.SIZE_INT:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.Signed)), array)
            res = args[2].getint()
            p[index] = res
        elif itemsize_index == self.SIZE_CHAR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.Char)), array)
            res = chr(args[2].getint())
            p[index] = res
        elif itemsize_index == self.SIZE_UNICHAR:
            p = rffi.cast(lltype.Ptr(lltype.GcArray(lltype.UniChar)), array)
            res = unichr(args[2].getint())
            p[index] = res
        else:
            raise BadSizeError

    @specialize.argtype(1)
    def _do_setfield(self, struct, v_value, fielddescr):
        assert isinstance(fielddescr, FieldDescr)
        size_index = fielddescr.size_index
        if size_index == self.SIZE_GCPTR:
            p = rffi.cast(rffi.CArrayPtr(llmemory.GCREF), struct)
            res = v_value.getptr_base()
            p[fielddescr.offset / rffi.sizeof(llmemory.GCREF)] = res
        elif size_index == self.SIZE_INT:
            p = rffi.cast(rffi.CArrayPtr(lltype.Signed), struct)
            res = v_value.getint()
            p[fielddescr.offset / rffi.sizeof(lltype.Signed)] = res
        elif size_index == self.SIZE_CHAR:
            p = rffi.cast(rffi.CArrayPtr(lltype.Char), struct)
            res = chr(v_value.getint())
            p[fielddescr.offset / rffi.sizeof(lltype.Char)] = res
        elif size_index == self.SIZE_UNICHAR:
            p = rffi.cast(rffi.CArrayPtr(lltype.UniChar), struct)
            res = unichr(v_value.getint())
            p[fielddescr.offset / rffi.sizeof(lltype.UniChar)] = res
        else:
            raise BadSizeError

    def do_setfield_gc(self, args, fielddescr):
        struct = args[0].getptr_base()
        self._do_setfield(struct, args[1], fielddescr)

    def do_setfield_raw(self, args, fielddescr):
        struct = args[0].getaddr(self)
        self._do_setfield(struct, args[1], fielddescr)

    def do_newstr(self, args, descr=None):
        return self._allocate_new_array(args, 1,
                                        self.string_index_array,
                                        self.string_index_length)

    def do_newunicode(self, args, descr=None):
        return self._allocate_new_array(args, self.size_of_unicode,
                                        self.unicode_index_array,
                                        self.unicode_index_length)

    def do_strsetitem(self, args, descr=None):
        s = args[0].getptr_base()
        res = chr(args[2].getint())
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.STR), s)
        p.chars[args[1].getint()] = res

    def do_unicodesetitem(self, args, descr=None):
        s = args[0].getptr_base()
        res = unichr(args[2].getint())
        p = lltype.cast_opaque_ptr(lltype.Ptr(rstr.UNICODE), s)
        p.chars[args[1].getint()] = res

    def _new_box(self, ptr):
        if ptr:
            return BoxPtr(lltype.nullptr(llmemory.GCREF.TO))
        return BoxInt(0)

    def _get_loop_for_call(self, argnum, calldescr, ptr):
        loop = calldescr._generated_mp
        if loop is None:
            args = [BoxInt() for i in range(argnum + 1)]
            result = self._new_box(ptr)
            operations = [
                ResOperation(rop.CALL, args, result, calldescr),
                ResOperation(rop.GUARD_NO_EXCEPTION, [], None),
                ResOperation(rop.FAIL, [result], None)]
            operations[1].suboperations = [ResOperation(rop.FAIL, [], None)]
            loop = history.TreeLoop('call')
            loop.inputargs = args
            loop.operations = operations
            self.compile_operations(loop)
            calldescr._generated_mp = loop
        return loop

    def do_call(self, args, calldescr):
        assert isinstance(calldescr, CallDescr)
        num_args = len(args) - 1
        ptr = (calldescr.result_mask == -2)
        loop = self._get_loop_for_call(num_args, calldescr, ptr)
        history.set_future_values(self, args)
        self.execute_operations(loop)
        # Note: if an exception is set, the rest of the code does a bit of
        # nonsense but nothing wrong (the return value should be ignored)
        if calldescr.result_mask == 0:
            return None
        elif ptr:
            return BoxPtr(self.get_latest_value_ptr(0))
        else:
            return BoxInt(self.get_latest_value_int(0))

    def do_cast_int_to_ptr(self, args, descr=None):
        int = args[0].getint()
        res = rffi.cast(llmemory.GCREF, int)
        return BoxPtr(res)

    def do_cast_ptr_to_int(self, args, descr=None):
        ptr = args[0].getptr_base()
        res = rffi.cast(lltype.Signed, ptr)
        return BoxInt(res)


class SizeDescr(AbstractDescr):
    def __init__(self, size):
        self.size = size

class FieldDescr(AbstractDescr):
    def __init__(self, offset, size_index):
        self.offset = offset
        self.size_index = size_index    # index in cpu.types_by_index

class ArrayDescr(AbstractDescr):
    def __init__(self, itemsize, itemsize_index):
        self.itemsize = itemsize
        self.itemsize_index = itemsize_index   # index in cpu.types_by_index
        self.ty_array_ptr = lltype.nullptr(llvm_rffi.LLVMTypeRef.TO)
        # ^^^ set by setup_once()

class CallDescr(AbstractDescr):
    def __init__(self, ty_function_ptr, result_mask):
        self.ty_function_ptr = ty_function_ptr
        self.result_mask = result_mask     # -2 to mark a ptr result
        self._generated_mp = None

# ____________________________________________________________

class BadSizeError(Exception):
    pass

import pypy.jit.metainterp.executor
pypy.jit.metainterp.executor.make_execute_list(LLVMCPU)
