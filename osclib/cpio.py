#!/usr/bin/python3

import struct


class Cpio(object):
    def __init__(self, buf):
        self.buf = buf
        self.off = 0

    def __iter__(self):
        return self

    def next(self):
        f = CpioFile(self.off, self.buf)
        if f.fin():
            raise StopIteration
        self.off = self.off + f.length()
        return f


class CpioFile(object):
    def __init__(self, off, buf):
        self.off = off
        self.buf = buf

        if (off & 3):
            raise Exception("invalid offset %d" % off)

        fmt = "6s8s8s8s8s8s8s8s8s8s8s8s8s8s"
        off = self.off + struct.calcsize(fmt)

        fields = struct.unpack(fmt, buf[self.off:off])

        if fields[0] != "070701":
            raise Exception(f"invalid cpio header {self.c_magic}")

        names = ("c_ino", "c_mode", "c_uid", "c_gid",
                 "c_nlink", "c_mtime", "c_filesize",
                 "c_devmajor", "c_devminor", "c_rdevmajor",
                 "c_rdevminor", "c_namesize", "c_check")
        for (n, v) in zip(names, fields[1:]):
            setattr(self, n, int(v, 16))

        nlen = self.c_namesize - 1
        self.name = struct.unpack('%ds' % nlen, buf[off:off + nlen])[0]
        off = off + nlen + 1
        if (off & 3):
            off = off + 4 - (off & 3)   # padding
        self.payloadstart = off

    def fin(self):
        return self.name == 'TRAILER!!!'

    def __str__(self):
        return "[%s %d]" % (self.name, self.c_filesize)

    def header(self):
        return self.buf[self.payloadstart:self.payloadstart + self.c_filesize]

    def length(self):
        len = self.payloadstart - self.off + self.c_filesize
        if (self.c_filesize & 3):
            len = len + 4 - (self.c_filesize & 3)
        return len


if __name__ == '__main__':
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option("--debug", action="store_true", help="debug output")
    parser.add_option("--verbose", action="store_true", help="verbose")

    (options, args) = parser.parse_args()

    for fn in args:
        fh = open(fn, 'rb')
        cpio = Cpio(fh.read())
        for i in cpio:
            print(i)
            with open(i.name, 'wb') as ofh:
                ofh.write(i.header())
