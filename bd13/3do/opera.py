import sys, struct
class Disc:
    def __init__(s, path):
        s.f = open(path,'rb')
        sync = s.f.read(12)                         # raw 2352-byte sectors (.bin) start with the sync pattern
        s.ss, s.off = (2352, 16) if sync == bytes([0]) + bytes([255]) * 10 + bytes([0]) else (2048, 0)
        h = s.sec(0)
        (s.bs, s.cnt, s.rootid, s.rootblocks, s.rootbs, s.rootlast) = struct.unpack('>6I', h[76:100])
        s.root = struct.unpack('>I', h[100:104])[0]
        s.label = h[40:72].rstrip(b'\0')
    def sec(s, i):
        s.f.seek(i*s.ss+s.off); return s.f.read(2048)
    def read(s, start, size):
        out = bytearray(); i = start
        while len(out) < size:
            out += s.sec(i); i += 1
        return bytes(out[:size])
    def listdir(s, blk, size, path=''):
        res=[]
        buf = s.read(blk, size)
        for b0 in range(0, len(buf), 2048):
            blkbuf = buf[b0:b0+2048]
            nxt,prev,fl,ffb,off = struct.unpack('>iiiii', blkbuf[:20])
            p = off
            while p+72 <= ffb:
                flg,ident,typ,bsz,nbytes,nblk,burst,gap = struct.unpack('>8I', blkbuf[p:p+32])
                name = blkbuf[p+32:p+64].split(b'\0')[0].decode('latin1')
                last = struct.unpack('>I', blkbuf[p+64:p+68])[0]
                ptr = struct.unpack('>I', blkbuf[p+68:p+72])[0]
                res.append((flg&0xff, path+name, ptr, nbytes, bsz, nblk, typ))
                p += 72+4*last
                if flg & 0x80000000: break
                if flg & 0x40000000: break
        return res
    def tree(s):
        out=[]
        def rec(blk,size,path):
            for e in s.listdir(blk,size,path):
                t,name,ptr,nb,bsz,nblk,typ=e
                if t in (6,7):
                    rec(ptr, nb if nb else nblk*bsz, name+'/')
                    out.append(e)
                else: out.append(e)
        rec(s.root, s.rootblocks*s.rootbs if s.rootbs else 2048, '')
        return out
if __name__=='__main__':
    d=Disc(sys.argv[1]); print(d.label, d.bs, d.cnt, d.root, d.rootblocks, d.rootbs)
    for e in d.tree(): print(e)
