import os, subprocess, cStringIO, marshal, time, sys, cPickle
import httplib, re, traceback, tempfile, struct, urllib, random
from disco.util import parse_dir, load_conf
from disco.func import netstr_reader, re_reader
from disco.netstring import *

from disconode import external, util
from disconode.util import *

job_name = ""
http_pool = {}

status_interval = 0

def init():
        global HTTP_PORT, LOCAL_PATH, PARAMS_FILE, EXT_MAP, EXT_REDUCE,\
               MAP_OUTPUT, CHUNK_OUTPUT, REDUCE_DL, REDUCE_SORTED, REDUCE_OUTPUT

        tmp, HTTP_PORT, LOCAL_PATH = load_conf()

        PARAMS_FILE = LOCAL_PATH + "%s/params"
        EXT_MAP = LOCAL_PATH + "%s/ext-map"
        EXT_REDUCE = LOCAL_PATH + "%s/ext-reduce"
        MAP_OUTPUT = LOCAL_PATH + "%s/map-disco-%d-%.9d"
        CHUNK_OUTPUT = LOCAL_PATH + "%s/map-chunk-%d"
        REDUCE_DL = LOCAL_PATH + "%s/reduce-in-%d.dl"
        REDUCE_SORTED = LOCAL_PATH + "%s/reduce-in-%d.sorted"
        REDUCE_OUTPUT = LOCAL_PATH + "%s/reduce-disco-%d"

def this_host():
        return sys.argv[3]

def this_partition():
        return int(sys.argv[5])
        
def this_inputs():
        return sys.argv[6:]

def open_local(input, fname, is_chunk):
        try:
                f = file(fname)
                if is_chunk:
                        f.seek(this_partition() * 8)
                        start, end = struct.unpack("QQ", f.read(16))
                        sze = end - start
                        f.seek(start)
                else:
                        sze = os.stat(fname).st_size
                return sze, f
        except:
                data_err("Can't access a local input file: %s"\
                                % input, input)

def open_remote(input, ext_host, ext_file, is_chunk):
        try:
                # We can't open a new HTTP connection for each intermediate
                # result -- this would result to M * R TCP connections where
                # M is the number of maps and R the number of reduces. Instead,
                # we pool connections and reuse them whenever possible. HTTP 
                # 1.1 defaults to keep-alive anyway.
                if ext_host in http_pool:
                        http = http_pool[ext_host]
                        if http._HTTPConnection__response:
                                http._HTTPConnection__response.read()
                else:
                        http = httplib.HTTPConnection(ext_host)
                        http_pool[ext_host] = http

                if is_chunk:
                        pos = this_partition() * 8
                        rge = "bytes=%d-%d" % (pos, pos + 15)
                        #msg("Reading offsets at %s" % rge)
                        http.request("GET", ext_file, None, {"Range": rge})
                        fd = http.getresponse()

                        if fd.status != 206:
                                raise "HTTP error %d" % fd.status
                        start, end = struct.unpack("QQ", fd.read())
                        if start == end:
                                return 0, cStringIO.StringIO()
                        else:
                                rge = "bytes=%d-%d" % (start, end - 1)
                        #msg("Reading data at %s" % rge)
                        http.request("GET", ext_file, None, {"Range": rge})
                        fd = http.getresponse()
                        if fd.status != 206:
                                raise "HTTP error %d" % fd.status
                else:
                        http.request("GET", ext_file, "")
                        fd = http.getresponse()
                        if fd.status != 200:
                                raise "HTTP error %d" % fd.status
                sze = fd.getheader("content-length")
                if sze:
                        sze = int(sze)
                return sze, fd

        except httplib.BadStatusLine:
                # BadStatusLine is caused by a closed connection. Re-open a new
                # connection by deleting this connection from the pool and
                # calling this function again. Note that this might result in
                # endless recursion if something went seriously wrong.
                http.close()
                del http_pool[ext_host]
                return open_remote(input, ext_host, ext_file, is_chunk)
        except:
                data_err("Can't access an external input file (%s/%s): %s"\
                                % (ext_host, ext_file, input), input)

def connect_input(input):
        if input.startswith("raw://"):
            return len(input)-6, cStringIO.StringIO(input[6:])

        is_chunk = input.startswith("chunk://")
        if input.startswith("disco://") or is_chunk:
                host, fname = input[8:].split("/", 1)
                local_file = LOCAL_PATH + fname
                ext_host = "%s:%s" % (host, HTTP_PORT)
                ext_file = "/" + fname
        elif input.startswith("http://"):
                ext_host, fname = input[7:].split("/", 1)
                host = ext_host
                ext_file = "/" + fname
                local_file = None
        else:
                host = this_host()
                if input.startswith("chunkfile://"):
                        is_chunk = True
                        local_file = input[12:]
                elif input.startswith("file://"):
                        local_file = input[7:]
                else:
                        local_file = input

        if host == this_host() and local_file:
                return open_local(input, local_file, is_chunk)
        else:
                return open_remote(input, ext_host, ext_file, is_chunk)

def encode_kv_pair(fd, key, value):
        skey = str(key)
        sval = str(value)
        fd.write("%d %s %d %s\n" % (len(skey), skey, len(sval), sval))


class MapOutput:
        def __init__(self, part, params, combiner = None):
                self.combiner = combiner
                self.params = params
                self.comb_buffer = {}
                self.fname = MAP_OUTPUT % (job_name, this_partition(), part)
                ensure_path(self.fname, False)
                self.fd = file(self.fname + ".partial", "w")
                self.part = part
                
        def add(self, key, value):
                if self.combiner:
                        ret = self.combiner(key, value, self.comb_buffer,\
                                   0, self.params)
                        if ret:
                                for key, value in ret:
                                        encode_kv_pair(self.fd, key, value)
                else:
                        encode_kv_pair(self.fd, key, value)

        def close(self):
                if self.combiner:
                        ret = self.combiner(None, None, self.comb_buffer,\
                                1, self.params)
                        if ret:
                                for key, value in ret:
                                        encode_kv_pair(self.fd, key, value)
                self.fd.close()
                os.rename(self.fname + ".partial", self.fname)
        
        def disco_address(self):
                return "disco://%s/%s" %\
                        (this_host(), self.fname[len(LOCAL_PATH):])


class ReduceOutput:
        def __init__(self):
                self.fname = REDUCE_OUTPUT % (job_name, this_partition())
                ensure_path(self.fname, False)
                self.fd = file(self.fname + ".partial", "w")

        def add(self, key, value):
                encode_kv_pair(self.fd, key, value)

        def close(self):
                self.fd.close()
                os.rename(self.fname + ".partial", self.fname)
        
        def disco_address(self):
                return "disco://%s/%s" %\
                        (this_host(), self.fname[len(LOCAL_PATH):])

def num_cmp(x, y):
        try:
                x = (int(x[0]), x[1])
                y = (int(y[0]), y[1])
        except ValueError:
                pass
        return cmp(x, y)

class ReduceReader:
        def __init__(self, input_files, do_sort, mem_sort_limit):
                self.inputs = []
                for input in input_files:
                        if input.startswith("dir://"):
                                self.inputs += parse_dir(input)
                        else:
                                self.inputs.append(input)

                self.line_count = 0
                if do_sort:
                        total_size = 0
                        for input in self.inputs:
                                sze, fd = connect_input(input)
                                total_size += sze

                        msg("Reduce[%d] input is %.2fMB" %\
                                (this_partition(), total_size / 1024.0**2))

                        if total_size > mem_sort_limit:
                                self.iterator = self.download_and_sort()
                        else: 
                                msg("Sorting in memory")
                                m = list(self.multi_file_iterator(self.inputs, False))
                                m.sort(num_cmp)
                                self.iterator = self.list_iterator(m)
                else:
                        self.iterator = self.multi_file_iterator(self.inputs)
                        
        def iter(self):
                return self.iterator

        def download_and_sort(self):
                dlname = REDUCE_DL % (job_name, this_partition())
                ensure_path(dlname, False)
                msg("Reduce will be downloaded to %s" % dlname)
                out_fd = file(dlname + ".partial", "w")
                for fname in self.inputs:
                        sze, fd = connect_input(fname)
                        for k, v in netstr_reader(fd, sze, fname):
                                if " " in k:
                                        err("Spaces are not allowed in keys "\
                                            "with external sort.")
                                if "\0" in v:
                                        err("Zero bytes are not allowed in "\
                                            "values with external sort. "\
                                            "Consider using base64 encoding.")
                                out_fd.write("%s %s\0" % (k, v))
                out_fd.close()
                os.rename(dlname + ".partial", dlname)
                msg("Reduce input downloaded ok")

                msg("Starting external sort")
                sortname = REDUCE_SORTED % (job_name, this_partition())
                ensure_path(sortname, False)
                cmd = ["sort", "-n", "-s", "-k", "1,1", "-z",\
                        "-t", " ", "-o", sortname, dlname]

                proc = subprocess.Popen(cmd)
                ret = proc.wait()
                if ret:
                        err("Sorting %s to %s failed (%d)" %\
                                (dlname, sortname, ret))
                
                msg("External sort done: %s" % sortname)
                return self.multi_file_iterator([sortname], reader =\
                        lambda fd, sze, fname:\
                                re_reader("(.*?) (.*?)\000", fd, sze, fname))

       
        def list_iterator(self, lst):
                i = 0
                for x in lst:
                        yield x
                        i += 1
                        if status_interval and not i % status_interval:
                                msg("%d entries reduced" % i)
                msg("Reduce done: %d entries reduced in total" % i)

        def multi_file_iterator(self, inputs, progress = True,
                                reader = netstr_reader):
                i = 0
                for fname in inputs:
                        sze, fd = connect_input(fname)
                        for x in reader(fd, sze, fname):
                                yield x
                                i += 1
                                if progress and status_interval and\
                                        not i % status_interval:
                                        msg("%d entries reduced" % i)

                if progress:
                        msg("Reduce done: %d entries reduced in total" % i)

# Function stubs

def fun_map(e, params):
        pass

def fun_map_reader(fd, sze, job_input):
        pass

def fun_partition(key, nr_reduces, params):
        pass

def fun_combiner(key, value, comb_buffer, flush, params):
        pass

def fun_reduce(red_in, red_out, params):
        pass

def run_map(job_input, partitions, param):
        i = 0
        sze, fd = connect_input(job_input)
        nr_reduces = len(partitions)
        
        for entry in fun_map_reader(fd, sze, job_input):
                for key, value in fun_map(entry, param):
                        p = fun_partition(key, nr_reduces, param)
                        partitions[p].add(key, value)
                i += 1
                if status_interval and not i % status_interval:
                        msg("%d entries mapped" % i)

        msg("Done: %d entries mapped in total" % i)

def merge_chunks(partitions):
        mapout = CHUNK_OUTPUT % (job_name, this_partition())
     
        f = file(mapout + ".partial", "w")
        offset = (len(partitions) + 1) * 8
        for p in partitions:
                f.write(struct.pack("Q", offset))
                offset += os.stat(p.fname).st_size
        f.write(struct.pack("Q", offset))
        f.close()

        if subprocess.call("cat %s >> %s.partial" % 
                        (" ".join([p.fname for p in partitions]),
                                mapout), shell = True):
                data_err("Couldn't create a chunk", mapout)
        os.rename(mapout + ".partial", mapout)
        for p in partitions:
                os.remove(p.fname)

def op_map(job):
        global job_name
        
        job_input = this_inputs()
        msg("Received a new map job!")
        
        if len(job_input) != 1:
                err("Map can only handle one input. Got: %s" % 
                        " ".join(job_input))

        nr_reduces = int(job['nr_reduces'])
        required_modules = job['required_modules'].split()
        fun_map_reader.func_code = marshal.loads(job['map_reader'])
        fun_partition.func_code = marshal.loads(job['partition'])
        for m in required_modules:
                fun_map_reader.func_globals.setdefault(m, __import__(m))
                fun_partition.func_globals.setdefault(m, __import__(m))
        
        if 'ext_map' in job:
                if 'ext_params' in job:
                        map_params = job['ext_params']
                else:
                        map_params = "0\n"
                external.prepare(job['ext_map'],
                        map_params, EXT_MAP % job_name)
                fun_map.func_code = external.ext_map.func_code
        else:
                map_params = cPickle.loads(job['params'])        
                fun_map.func_code = marshal.loads(job['map'])
        
        for m in required_modules:
                fun_map.func_globals.setdefault(m, __import__(m))

        if 'combiner' in job:
                fun_combiner.func_code = marshal.loads(job['combiner'])
                for m in required_modules:
                        fun_combiner.func_globals.setdefault(m, __import__(m))
                partitions = [MapOutput(i, map_params, fun_combiner)\
                        for i in range(nr_reduces)]
        else:
                partitions = [MapOutput(i, map_params) for i in range(nr_reduces)]
        
        run_map(job_input[0], partitions, map_params)
        for p in partitions:
                p.close()
        if 'chunked' in job:
                merge_chunks(partitions)
                out = "chunk://%s/%s/map-chunk-%d" %\
                        (this_host(), job_name, this_partition())
        else:
                out = partitions[0].disco_address()
        
        external.close_ext()
        msg("%d %s" % (this_partition(), out), "OUT")

def op_reduce(job):
        global job_name

        job_inputs = this_inputs()

        msg("Received a new reduce job!")
        
        do_sort = int(job['sort'])
        mem_sort_limit = int(job['mem_sort_limit'])
        required_modules = job['required_modules'].split()
        
        if 'ext_reduce' in job:
                if "ext_params" in job:
                        red_params = job['ext_params']
                else:
                        red_params = "0\n"
                external.prepare(job['ext_reduce'], red_params,
                        EXT_REDUCE % job_name)
                fun_reduce.func_code = external.ext_reduce.func_code
        else:
                fun_reduce.func_code = marshal.loads(job['reduce'])
                red_params = cPickle.loads(job['params'])

        for m in required_modules:
                fun_reduce.func_globals.setdefault(m, __import__(m))

        red_in = ReduceReader(job_inputs, do_sort, mem_sort_limit)
        red_out = ReduceOutput()
        msg("Starting reduce")
        fun_reduce(red_in.iter(), red_out, red_params)
        msg("Reduce done")
        red_out.close()
        external.close_ext()

        msg("%d %s" % (this_partition(), red_out.disco_address()), "OUT")

init()
