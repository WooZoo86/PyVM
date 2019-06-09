import logging
import os
import struct

from .util import segment_descriptor_struct

logger = logging.getLogger(__name__)

struct_iovec = struct.Struct('<II')
struct_user_desc = struct.Struct('<ILIBH4B')

from ctypes import LittleEndianStructure, c_uint32, sizeof

udword = c_uint32.__ctype_le__


class structUserDesc(LittleEndianStructure):
    _pack_ = 1
    _fields_ = [
        ('entry_number', udword),
        ('base_addr', udword),
        ('limit', udword),
        ('seg_32bit', udword, 1),
        ('contents', udword, 2),
        ('read_exec_only', udword, 1),
        ('limit_in_pages', udword, 1),
        ('seg_not_present', udword, 1),
        ('useable', udword, 1),

    ]

    def __str__(self):
        return """struct user_desc {
    unsigned int  entry_number      = 0x%08x;
    unsigned long base_addr         = 0x%08x;
    unsigned int  limit             = 0x%08x;
    unsigned int  seg_32bit:1       = %u;
    unsigned int  contents:2        = 0x%02x;
    unsigned int  read_exec_only:1  = %u;
    unsigned int  limit_in_pages:1  = %u;
    unsigned int  seg_not_present:1 = %u;
    unsigned int  useable:1         = %u;
};""" % (self.entry_number, self.base_addr, self.limit, self.seg_32bit, self.contents,
         self.read_exec_only, self.limit_in_pages, self.seg_not_present, self.useable)

from io import UnsupportedOperation


class SyscallsMixin_Meta(type):
    def __new__(cls, name, bases, dict):
        syscalls = {
            y.__defaults__[0]: y
            for x, y in dict.items()
            if x.startswith('sys_')
        }

        for syscall in syscalls.values():
            dict[syscall.__name__] = syscall

        dict['valid_syscalls_names'] = {code: fn.__name__ for code, fn in syscalls.items()}

        # make `type` the metaclass, otherwise there'll be a metaclass conflict
        return type(name, bases, dict)


class SyscallsMixin(metaclass=SyscallsMixin_Meta):
    def __read_string(self, address: int):
        # TODO: maybe use `ctypes.string_at`?

        ret = bytearray()
        
        byte = self.mem.get(address, 1)
        while byte != 0:
            ret.append(byte)
            address += 1
            byte = self.mem.get(address, 1)
            
        return ret

    def __return(self, value: int):
        self.reg.eax = value

    def __args(self, types: str):
        """

        :param types: Indicates signed ('s') or unsigned types.
        Example:
            'sus' => arguments 1 and 3 are signed, argument 2 is unsigned
        :return:
        """
        registers = [3, 1, 2, 6, 7]  # ebx, ecx, edx, esi, edi

        return [self.reg.get(reg, 4) for reg, type in zip(registers, types)]

    def sys_exit(self, code=0x01):
        code, = self.__args('s')

        # deallocate memory
        self.mem.program_break = self.code_segment_end

        self.descriptors[2].write(f'[!] Process exited with code {code}\n')
        self.RETCODE = code
        self.running = False

    def sys_read(self, code=0x03):
        fd, data_addr, count = self.__args('uuu')

        logger.info('sys_read(unsigned int fd = %u, char *dest = 0x%08x, size_t count = %u)', fd, data_addr, count)

        try:
            data = os.read(self.descriptors[fd].fileno(), count)
        except (AttributeError, UnsupportedOperation):
            data = (self.descriptors[fd].read(count) + '\n').encode('ascii')
        except:
            logger.info('\tsys_read: [ERR] failed to read %u bytes from descriptor %u', count, fd)

            return self.__return(-1)

        logger.info('\tsys_read: [SUCC] read %r from fd %u', data, fd)

        l = len(data)
        self.mem.set_bytes(data_addr, l, data)

        self.__return(l)

    def sys_write(self, code=0x04):
        """
        Arguments: (unsigned int fd, const char * buf, size_t count)
        """
        fd, buf_addr, count = self.__args('uuu')

        buf = self.mem.get_bytes(buf_addr, count)

        logger.info('sys_write(%d, 0x%08x(%s), %d)', fd, buf_addr, buf, count)
        try:
            fileno = self.descriptors[fd].fileno()
            ret = os.write(fileno, buf)
            # os.fsync(fileno)
        except (AttributeError, UnsupportedOperation):
            ret = self.descriptors[fd].write(buf.decode('ascii'))
            self.descriptors[fd].flush()

        size = ret if ret is not None else count

        self.__return(size)

    def sys_brk(self, code=0x2d):
        '''
        Arguments: (unsigned long brk)

        https://elixir.bootlin.com/linux/v2.6.35/source/mm/mmap.c#L245
        '''
        brk, = self.__args('u')

        logger.info('sys_brk(unsigned long brk = %u)', brk)

        min_brk = self.code_segment_end

        if brk < min_brk:
            logger.info(
                '\tsys_brk: [SUCC] invalid break: 0x%08x < 0x%08x; return 0x%08x',
                brk, min_brk, self.mem.program_break
            )

            return self.__return(self.mem.program_break)

        newbrk = brk
        oldbrk = self.mem.program_break

        if oldbrk == newbrk:
            logger.info('\tsys_brk: [SUCC] not changing break: 0x%08x == 0x%08x', oldbrk, newbrk)

            return self.__return(self.mem.program_break)

        self.mem.program_break = brk

        logger.info('\tsys_brk: [SUCC] changing break: 0x%08x -> 0x%08x (%+d bytes)',
            oldbrk, self.mem.program_break, self.mem.program_break - oldbrk
        )

        self.__return(self.mem.program_break)

    def sys_set_thread_area(self, code=0xf3):
        """
        Arguments: (struct user_desc *u_info)

        Docs: http://man7.org/linux/man-pages/man2/set_thread_area.2.html

        // TAKEN FROM: http://man7.org/linux/man-pages/man2/set_thread_area.2.html
        struct user_desc {
            unsigned int  entry_number;
            unsigned long base_addr;
            unsigned int  limit;
            unsigned int  seg_32bit:1;
            unsigned int  contents:2;
            unsigned int  read_exec_only:1;
            unsigned int  limit_in_pages:1;
            unsigned int  seg_not_present:1;
            unsigned int  useable:1;
        };
        """
        u_info_addr, = self.__args('u')
        
        logger.info(f'sys_set_thread_area(u_info=0x%08x)', u_info_addr)

        raw_data = self.mem.get_bytes(u_info_addr, struct_user_desc.size)

        # print(f'Raw data: {list(raw_data)}, {sizeof(structUserDesc)}')

        # u_info = struct_user_desc.unpack(raw_data)
        u_info = structUserDesc.from_buffer_copy(raw_data)

        logger.info('%s', u_info)

        """
        A `user_desc` is considered "empty" if `read_exec_only` and
       `seg_not_present` are set to 1 and all of the other fields are 0.  If
       an "empty" descriptor is passed to `set_thread_area()`, the correspond‐
       ing TLS entry will be cleared.
        """

        selector_index = 0
        if u_info.entry_number == 0xffffffff:  # a.k.a. (unsigned int)(-1)
            """
            When set_thread_area() is passed an entry_number of -1, it searches
            for a free TLS entry.  If set_thread_area() finds a free TLS entry,
            the value of u_info->entry_number is set upon return to show which
            entry was changed.
            """

            from .Registers_ctypes import SegmentDescriptor
            for selector_index, entry in enumerate(self.GDT[1:], 1):
                seg_descr = SegmentDescriptor.from_buffer_copy(entry)  #segment_descriptor_struct.unpack(entry)

                if seg_descr.P:  # segment present
                    continue

                # BEGIN set up BASE
                seg_descr.base_1 = u_info.base_addr & 0xFFFF
                seg_descr.base_2 = (u_info.base_addr >> 16) & 0xFF
                seg_descr.base_3 = (u_info.base_addr >> 24) & 0xFF
                # END set up BASE

                # BEGIN set up LIMIT
                seg_descr.limit_1 = u_info.limit & 0xFFFF
                seg_descr.limit_2 = (u_info.limit >> 16) & 0xF
                # END set up LIMIT

                seg_descr.P = 1  # set segment present to True

                self.GDT[selector_index] = bytes(seg_descr)  # segment_descriptor_struct.pack(*descriptor)

                # print(f'Written segment descriptor: {seg_descr}')

                break

        self.mem.set(u_info_addr, 4, selector_index)  # set address of new selector
        # return success (0) or error (-1)
        self.__return(0)
        
    def sys_modify_ldt(self, code=0x7b):
        """
        Arguments: (int func, void *ptr, unsigned long bytecount)

        modify_ldt() reads or writes the local descriptor table (LDT) for a
       process.
        """

        func, ptr_addr, bytecount = self.__args('uuu')

        logger.info(f'modify_ldt(func={func}, ptr={ptr_addr:04x}, bytecount={bytecount})')
        # do nothing, return error
        self.__return(-1)
        
    def sys_set_tid_address(self, code=0x102):
        """
        Arguments: (int *tidptr)

        The system call set_tid_address() sets the clear_child_tid value for
       the calling thread to tidptr.

        :return: always returns the caller's thread ID.
        """
        tidptr, = self.__args('u')

        tid = self.mem.get(tidptr, 4)

        logger.info('sys_set_tid_address(tidptr=0x%08x (tid=%d))', tidptr, tid)

        # do nothing, return tid (thread ID)
        self.__return(tid)

    def sys_exit_group(self, code=0xfc):
        return self.sys_exit()

    def sys_writev(self, code=0x92):
        """
        ssize_t writev(
            int fd,  // file descriptor
            const struct iovec *iov,  // pointer to the beginning of an _array_ of structs
            int iovcnt  // number of elements in that array
            );

        The `writev()` system call writes `iovcnt` buffers of data described by
        `iov` to the file associated with the file descriptor `fd` ("gather output").

        TAKEN FROM: http://man7.org/linux/man-pages/man2/writev.2.html

        struct iovec
        {
            void * iov_base; / * Starting address * /
            size_t iov_len; / * Number of bytes to transfer * /
        };
        """
        fd, iov_addr, iovcnt = self.__args('sus')

        logger.info('sys_writev(fd=%d, iov=0x%x, iovcnt=%d)', fd, iov_addr, iovcnt)

        size = 0
        for x in range(iovcnt):
            iov_base, iov_len = struct_iovec.unpack(
                self.mem.get_bytes(iov_addr, struct_iovec.size)
            )

            logger.debug('struct iovec {\n\tvoid *iov_base=0x%08x;\n\tsize_t iov_len=%d;\n}', iov_base, iov_len)

            if not iov_len:
                iov_addr += struct_iovec.size
                continue

            buf = self.mem.get_bytes(iov_base, iov_len)

            logger.debug('iov_%d=0x%08x; iov_len=%d, buf=%s', x, iov_base, iov_len, buf)

            try:
                ret = os.write(self.descriptors[fd].fileno(), buf)
            except (AttributeError, UnsupportedOperation):
                ret = self.descriptors[fd].write(buf.decode('ascii'))
                self.descriptors[fd].flush()

            size += ret if ret is not None else iov_len
            iov_addr += struct_iovec.size  # address of the next struct!

        self.__return(size)

    def sys_llseek(self, code=0x8c):
        """
        Arguments: (unsigned int fd, unsigned long offset_high,
                   unsigned long offset_low, loff_t *result,
                   unsigned int whence)

        See: http://man7.org/linux/man-pages/man2/llseek.2.html
        """

        fd, offset_high, offset_low, result_addr, whence = self.__args('uuuuu')

        logger.info('sys_lseek(fd=%d, offset_high=%d, offset_low=%d, result=0x%08x, whence=%d)',
                     fd, offset_high, offset_low, result_addr, whence
                     )

        offset = (offset_high << 32) | offset_low

        try:
            ret = os.lseek(self.descriptors[fd].fileno(), offset & 0xFFFFFFFF, whence)
        except OSError:
            return self.__return(-1)
        else:
            self.mem.set(result_addr, 4, ret)

        # return success
        self.__return(0)

    def sys_ioctl(self, code=0x36):
        """
        Arguments: (int fd, unsigned long request, ...)
        """

        fd, request, data_addr = self.__args('suu')

        # SOURCE: http://man7.org/linux/man-pages/man2/ioctl_list.2.html
        # < include / asm - i386 / termios.h >
        #
        # 0x00005401 TCGETS struct termios *
        # 0x00005402 TCSETS const struct termios *
        # 0x00005403 TCSETSW const struct termios *
        # 0x00005404 TCSETSF const struct termios *
        # 0x00005405 TCGETA struct termio *
        # 0x00005406 TCSETA const struct termio *
        # 0x00005407 TCSETAW const struct termio *
        # 0x00005408 TCSETAF const struct termio *
        # 0x00005409 TCSBRK int
        # 0x0000540A TCXONC int
        # 0x0000540B TCFLSH int
        # 0x0000540C TIOCEXCL void
        # 0x0000540D TIOCNXCL void
        # 0x0000540E TIOCSCTTY int
        # 0x0000540F TIOCGPGRP pid_t *
        # 0x00005410 TIOCSPGRP const pid_t *
        # 0x00005411 TIOCOUTQ int *
        # 0x00005412 TIOCSTI const char *
        # 0x00005413 TIOCGWINSZ struct winsize *
        # 0x00005414 TIOCSWINSZ const struct winsize *
        # 0x00005415 TIOCMGET int *
        # 0x00005416 TIOCMBIS const int *
        # 0x00005417 TIOCMBIC const int *
        # 0x00005418 TIOCMSET const int *
        # 0x00005419 TIOCGSOFTCAR int *
        # 0x0000541A TIOCSSOFTCAR const int *
        # 0x0000541B FIONREAD int *
        # 0x0000541B TIOCINQ int *
        # 0x0000541C TIOCLINUX const char * // MORE
        # 0x0000541D TIOCCONS void
        # 0x0000541E TIOCGSERIAL struct serial_struct *
        # 0x0000541F TIOCSSERIAL const struct serial_struct *
        # 0x00005420 TIOCPKT const int *
        # 0x00005421 FIONBIO const int *
        # 0x00005422 TIOCNOTTY void
        # 0x00005423 TIOCSETD const int *
        # 0x00005424 TIOCGETD int *
        # 0x00005425 TCSBRKP int
        # 0x00005426 TIOCTTYGSTRUCT struct tty_struct *
        # 0x00005450 FIONCLEX void
        # 0x00005451 FIOCLEX void
        # 0x00005452 FIOASYNC const int *
        # 0x00005453 TIOCSERCONFIG void
        # 0x00005454 TIOCSERGWILD int *
        # 0x00005455 TIOCSERSWILD const int *
        # 0x00005456 TIOCGLCKTRMIOS struct termios *
        # 0x00005457 TIOCSLCKTRMIOS const struct termios *
        # 0x00005458 TIOCSERGSTRUCT struct async_struct *
        # 0x00005459 TIOCSERGETLSR int *
        # 0x0000545A TIOCSERGETMULTI struct serial_multiport_struct *
        # 0x0000545B TIOCSERSETMULTI const struct serial_multiport_struct *

        import enum
        directions = enum.Flag('directions', '_IOC_NONE _IOC_READ _IOC_WRITE', start=0)

        # TAKEN FROM: https://elixir.bootlin.com/linux/v5.0.8/source/include/uapi/asm-generic/ioctl.h
        _IOC_NRBITS   = 8
        _IOC_TYPEBITS = 8
        _IOC_SIZEBITS = 14
        _IOC_DIRBITS  = 2

        _IOC_NRMASK = ((1 << _IOC_NRBITS)-1)
        _IOC_TYPEMASK = ((1 << _IOC_TYPEBITS)-1)
        _IOC_SIZEMASK = ((1 << _IOC_SIZEBITS)-1)
        _IOC_DIRMASK = ((1 << _IOC_DIRBITS)-1)

        _IOC_NRSHIFT = 0
        _IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
        _IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
        _IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

        _IOC_NONE = 0
        _IOC_WRITE = 1
        _IOC_READ = 2

        _IOC_DIR = lambda nr: (((nr) >> _IOC_DIRSHIFT) & _IOC_DIRMASK)
        _IOC_TYPE = lambda nr: (((nr) >> _IOC_TYPESHIFT) & _IOC_TYPEMASK)
        _IOC_NR = lambda nr: (((nr) >> _IOC_NRSHIFT) & _IOC_NRMASK)
        _IOC_SIZE = lambda nr: (((nr) >> _IOC_SIZESHIFT) & _IOC_SIZEMASK)

        IOC_IN = (_IOC_WRITE << _IOC_DIRSHIFT)
        IOC_OUT = (_IOC_READ << _IOC_DIRSHIFT)
        IOC_INOUT = ((_IOC_WRITE|_IOC_READ) << _IOC_DIRSHIFT)
        IOCSIZE_MASK = (_IOC_SIZEMASK << _IOC_SIZESHIFT)
        IOCSIZE_SHIFT = (_IOC_SIZESHIFT)

        request_type = bytes([_IOC_TYPE(request)])
        request_number = _IOC_NR(request)
        request_direction = directions(_IOC_DIR(request))
        request_size = _IOC_SIZE(request)

        logger.info(f'ioctl(fd={fd},request={request:09_x} (type={request_type}, number={request_number}, direction={request_direction}, size={request_size}))')

        if request_type == b'T':
            if request_number == 19 and request_direction == directions._IOC_NONE:
                try:
                    self.descriptors[fd]
                except IndexError:
                    return self.__return(-1)

                # TAKEN FROM: http://man7.org/linux/man-pages/man4/tty_ioctl.4.html
                #
                # struct winsize
                # {
                #     unsigned short ws_row;
                #     unsigned short ws_col;
                #     unsigned short ws_xpixel; / *unused * /
                #     unsigned short ws_ypixel; / *unused * /
                # };
                struct_winsize = struct.Struct('<HHHH')

                self.mem.set_bytes(data_addr, struct_winsize.size, struct_winsize.pack(256, 256, 0, 0))

                return self.__return(0)

        self.__return(-1)

    def sys_newuname(self, code=0x7a):
        """
        int sys_newuname(struct new_utsname *buf);

        // See: https://elixir.bootlin.com/linux/v2.6.35/source/include/linux/utsname.h#L24
        struct new_utsname {
            char sysname[__NEW_UTS_LEN + 1];
            char nodename[__NEW_UTS_LEN + 1];
            char release[__NEW_UTS_LEN + 1];
            char version[__NEW_UTS_LEN + 1];
            char machine[__NEW_UTS_LEN + 1];
            char domainname[__NEW_UTS_LEN + 1];
        };
        """

        buf_addr, = self.__args('u')

        logger.debug(f'sys_newuname(struct new_utsname *buf=0x%08X)', buf_addr)

        __NEW_UTS_LEN = 64
        struct_new_utsname = struct.Struct('<{0}s{0}s{0}s{0}s{0}s{0}s'.format(__NEW_UTS_LEN + 1))

        sysname = 'PyVM_Linux'.encode('ascii')
        nodename = 'PyVM_Linux'.encode('ascii')
        release = '3.14'.encode('ascii')
        version = '3.14'.encode('ascii')
        machine = 'PyVM - Intel IA-32 on Python'.encode('ascii')
        domainname = 'PyVM_Linux.local'.encode('ascii')

        buf = struct_new_utsname.pack(
            sysname, nodename, release, version, machine, domainname
        )

        self.mem.set(buf_addr, buf)

        self.__return(0)

    def sys_open(self, code=0x05):
        """
        int open(const char *pathname, int flags, mode_t mode);
        """

        import enum, os

        class O_MODE(enum.IntFlag):
            """
            File access modes.
            See: https://github.com/torvalds/linux/blob/master/include/uapi/asm-generic/fcntl.h
            """
            O_ACCMODE	= 0o00000003
            O_RDONLY	= 0o00000000
            O_WRONLY	= 0o00000001
            O_RDWR		= 0o00000002
            O_CREAT		= 0o00000100  # not fcntl
            O_EXCL		= 0o00000200  # not fcntl
            O_NOCTTY	= 0o00000400  # not fcntl
            O_TRUNC		= 0o00001000  # not fcntl
            O_APPEND	= 0o00002000
            O_NONBLOCK	= 0o00004000
            O_DSYNC		= 0o00010000  # used to be O_SYNC, see below
            FASYNC		= 0o00020000  # fcntl, for BSD compatibility
            O_DIRECT	= 0o00040000  # direct disk access hint
            O_LARGEFILE	= 0o00100000
            O_DIRECTORY	= 0o00200000  # must be a directory
            O_NOFOLLOW	= 0o00400000  # don't follow links
            O_NOATIME	= 0o01000000
            O_CLOEXEC	= 0o02000000  # set close_on_exec

            _O_SYNC    = 0o04000000
            O_SYNC		= (_O_SYNC | O_DSYNC)
            O_PATH		= 0o010000000

            _O_TMPFILE = 0o020000000
            # a horrid kludge trying to make sure that this will fail on old kernels
            O_TMPFILE   = (_O_TMPFILE | O_DIRECTORY)
            O_TMPFILE_MASK = (_O_TMPFILE | O_DIRECTORY | O_CREAT)

        def open_file(name: str, mode: str):
            # find empty descriptor, starting from 3
            descriptor = -1
            for descr, file in enumerate(self.descriptors):
                if file is None:
                    # found empty descriptor
                    # print('found empty descriptor')
                    descriptor = descr
                    break

            if descriptor == -1:  # no empty descriptors found
                descriptor = len(self.descriptors)
                # print(f'opening new descriptor: {descriptor}')
                self.descriptors.append(open(name, mode))
            else:
                # print(f'reusing existing descriptor: {descriptor}')
                self.descriptors[descriptor] = open(name, mode)

            return descriptor

        pathname_addr, flags, mode = self.__args('usu')

        pathname = self.__read_string(pathname_addr).decode()
        flags = O_MODE(flags)
        mode = O_MODE(mode)
        logger.info('sys_open(const char *pathname=%r, int flags=%s, mode_t mode=%s)', pathname, flags, mode)

        if flags & O_MODE.O_RDONLY:
            if not os.path.exists(pathname):
                return self.__return(-1)

            descr = open_file(pathname, 'r')
            logger.info('\tsys_open: [SUCC] %s descriptor %u', flags, descr)

            return self.__return(descr)
        elif flags & O_MODE.O_WRONLY:
            if flags & O_MODE.O_TRUNC:
                descr = open_file(pathname, 'w')
            else:
                descr = open_file(pathname, 'x')

            logger.info('\tsys_open: [SUCC] %s descriptor %u', flags, descr)

            return self.__return(descr)
        elif flags & O_MODE.O_RDWR:
            descr = open_file(pathname, 'r+')

            logger.info('\tsys_open: [SUCC] %s descriptor %u', flags, descr)

            return self.__return(descr)
        elif flags & O_MODE.O_LARGEFILE:  # open for reading?
            if pathname.startswith('/etc'):
                return self.__return(-1)

            descr = open_file(pathname, 'r')
            logger.info('\tsys_open: [SUCC] %s descriptor %u', flags, descr)

            return self.__return(descr)
        else:
            # TODO: do not know what to do with these yet...

            logger.info('\tsys_open: [ERR] %s do not know how to open', flags)
            return self.__return(-1)

    def sys_close(self, code=0x06):
        """
        sys_close(unsigned int fd)
        """

        fd, = self.__args('u')

        logger.info('sys_close(unsigned int fd = %u)', fd)

        if fd >= len(self.descriptors):
            logger.info('\tsys_close: [ERR] descriptor %u not found', fd)
            return self.__return(-1)  # error

        if self.descriptors[fd] is None:  #self.descriptors[fd].closed:
            logger.info('\tsys_close: [ERR] descriptor %u already closed', fd)
            return self.__return(-1)  # error

        self.descriptors[fd].close()
        self.descriptors[fd] = None

        logger.info('\tsys_close: [SUCC] descriptor %u closed', fd)

        self.__return(0)

    def sys_unlink(self, code=0x0a):
        """
        int sys_unlink(const char * pathname)
        """

        pathname_addr, = self.__args('u')
        pathname = self.__read_string(pathname_addr).decode()

        logger.info('sys_unlink(const char * pathname = %r)', pathname)

        try:
            ret = os.unlink(pathname)
        except OSError:
            ret = -1
            logger.info('\tsys_unlink: [ERR] failed to unlink %r', pathname)
        else:
            logger.info('\tsys_unlink: [SUCC] unlinked %r', pathname)

        self.__return(ret)

    def sys_mmap_pgoff(self, code=0xc0):
        """
        void *mmap2(void *addr, size_t length, int prot, int flags,
                  int fd, off_t offset);

        See: http://www.man7.org/linux/man-pages/man2/mmap2.2.html
        """

        import enum

        class MAP_FLAGS(enum.Flag):
            # see http://people.seas.harvard.edu/~apw/sreplay/src/linux/mmap.c
            MAP_SHARED    = 0x01   # Share changes
            MAP_PRIVATE   = 0x02   # Changes are private.
            MAP_FIXED     = 0x10   # Interpret addr exactly.
            MAP_FILE      = 0
            MAP_ANONYMOUS = 0x20   # Don't use a file.

        class MAP_PROT(enum.Flag):
            # see above
            PROT_READ  = 0x1  # Page can be read.
            PROT_WRITE = 0x2  # Page can be written.
            PROT_EXEC  = 0x4  # Page can be executed.
            PROT_NONE  = 0x0  # Page can not be accessed.

        addr, length, prot, flags, fd = self.__args('uusss')

        flags = MAP_FLAGS(flags)
        prot = MAP_PROT(prot)

        logger.info(
            'mmap(void *addr=0x%08x, size_t length=%d, int prot=%s, int flags=%s, int fd=%d, off_t offset=%d)',
            addr, length, str(prot), str(flags), fd, -1
        )

        if flags & MAP_FLAGS.MAP_ANONYMOUS:
            # TODO: Do something with protection?
            old_brk = self.mem.memset(self.mem.program_break, 0, length)
            self.mem.program_break += length

            logger.info('\tmmap: [SUCC] allocated %d bytes', length)

            return self.__return(old_brk)

        # TODO: mmap with file descriptors?

        logger.info('\tmmap: [ERR] unsupported call!')

        self.__return(-1)

    def sys_munmap(self, code=0x5b):
        """
        int munmap(void *addr, size_t length);
        """

        addr, length = self.__args('uu')

        logger.info('sys_munmap(void *addr = 0x%08x, size_t length = %u)', addr, length)

        if addr + length == self.mem.program_break:
            logger.info('\tsys_munmap: [SUCC] simple unmap')
            self.mem.program_break = addr
        else:
            logger.info('\tsys_munmap: [ERR] cannot simply unmap because %d bytes will be left', self.mem.program_break - (addr + length))

        logger.info('\tsys_munmap: [SUCC] returning success anyway')
        self.__return(0)

    def sys_rt_gprocmask(self, code=0xaf):
        """
        int rt_sigprocmask(int how, const kernel_sigset_t *set,
                          kernel_sigset_t *oldset, size_t sigsetsize);
        """

        self.__return(0)  # return success anyway

    def sys_tgkill(self, code=0x10e):
        """
        int tgkill(int tgid, int tid, int sig);

        `tgkill()` sends the signal `sig` to the thread with the thread ID `tid` in
       the thread group `tgid`.  (By contrast, kill(2) can be used to send a
       signal only to a process (i.e., thread group) as a whole, and the
       signal will be delivered to an arbitrary thread within that process.)
        """

        tgid, tid, sig = self.__args('sss')

        logging.debug('sys_tgkill(int tgid=%d, int tid=%d, int sig=%d)', tgid, tid, sig)

        self.__return(0)  # return success anyway

    def sys_sigaction(self, code=0xae):
        """
        int sigaction(int signum, const struct sigaction *act,
                     struct sigaction *oldact);

        See: http://man7.org/linux/man-pages/man2/rt_sigaction.2.html
        The `sigaction()` system call is used to change the action taken by a
       process on receipt of a specific signal.  (See signal(7) for an
       overview of signals.)
        """

        self.__return(-1)

    def sys_geteuid(self, code=0xc9):
        """
        uid_t geteuid(void);

        See: http://man7.org/linux/man-pages/man2/geteuid.2.html
        geteuid() returns the effective user ID of the calling process.
        """

        self.__return(os.geteuid())

    def sys_getuid(self, code=0xc7):
        """
        uid_t getuid(void);

        See: http://man7.org/linux/man-pages/man2/geteuid.2.html
        getuid() returns the real user ID of the calling process.
        """

        self.__return(os.getuid())

    def sys_getegid(self, code=0xca):
        """
        uid_t getegid(void);

        See: http://man7.org/linux/man-pages/man2/getegid.2.html
        getegid() returns the effective group ID of the calling process.
        """

        self.__return(os.getegid())

    def sys_getgid(self, code=0xc8):
        """
        uid_t getgid(void);

        See: http://man7.org/linux/man-pages/man2/getegid.2.html
        getgid() returns the real group ID of the calling process.
        """

        self.__return(os.getgid())

    def sys_readlink(self, code=0x55):
        """
        ssize_t readlink(const char *pathname, char *buf, size_t bufsiz);

        See: http://man7.org/linux/man-pages/man2/readlink.2.html
        `readlink()` places the contents of the symbolic link `pathname` in the
       buffer `buf`, which has size `bufsiz`.  `readlink()` does not append a null
       byte to `buf`.  It will (silently) truncate the contents (to a length
       of `bufsiz` characters), in case the buffer is too small to hold all of
       the contents.
        """

        pathname_addr, buf_addr, bufsiz = self.__args('uuu')

        pathname = self.__read_string(pathname_addr)
        logger.debug('sys_readlink(const char *pathname=%r, char *buf=0x%08x, size_t bufsiz=%d)', pathname.decode(), buf_addr, bufsiz)

        try:
            ret = os.readlink(pathname)
        except FileNotFoundError:
            return self.__return(-1)

        self.mem.set(buf_addr, ret)

        self.__return(0)

    def sys_clock_gettime(self, code=0x109):
        """
        int clock_gettime(clockid_t clk_id, struct timespec *tp);

        The functions clock_gettime() and clock_settime() retrieve and set
       the time of the specified clock clk_id.

       The res and tp arguments are timespec structures, as specified in
       <time.h>:

        struct timespec {
            time_t   tv_sec;        /* seconds */
            long     tv_nsec;       /* nanoseconds */
        };
        """

        clk_id, tp_addr = self.__args('uu')

        struct_timespec = struct.Struct('<II')

        import time

        time_nanoseconds = time.time_ns()
        sec, nsec = time_nanoseconds // 1_000_000_000, time_nanoseconds % 1_000_000_000

        self.mem.set_bytes(tp_addr, struct_timespec.size, struct_timespec.pack(sec, nsec))

        self.__return(0)
