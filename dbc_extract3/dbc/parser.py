import os, io, struct, sys, logging, math, re

import dbc.fmt

_BASE_HEADER = struct.Struct('IIII')
_DB_HEADER_1 = struct.Struct('III')
_DB_HEADER_2 = struct.Struct('IIIHH')
_WCH5_HEADER = struct.Struct('IIIIIII')
_ID          = struct.Struct('I')
_CLONE       = struct.Struct('II')
_ITEMRECORD  = struct.Struct('IH')
_WCH_ITEMRECORD = struct.Struct('IIH')
_WCH7_BASE_HEADER = struct.Struct('IIIII')

# WDB5 field data, size (32 - size) // 8, offset tuples
_FIELD_DATA  = struct.Struct('HH')

X_ID_BLOCK = 0x04
X_OFFSET_MAP = 0x01

class DBCParserBase:
    def is_magic(self):
        raise Exception()

    def __init__(self, options, fname):
        self.file_name_ = None
        self.options = options

        # Data format storage
        self.fmt = dbc.fmt.DBFormat(options)

        # Data stuff
        self.data = None
        self.parse_offset = 0
        self.data_offset = 0
        self.string_block_offset = 0

        # Bytes to set of byte fields handling
        self.field_data = []
        self.record_parser = None

        # Searching
        self.id_data = None

        # Parsing
        self.unpackers = []

        self.id_format_str = None

        # See that file exists already
        normalized_path = os.path.abspath(fname)
        for i in ['', '.db2', '.dbc', '.adb']:
            if os.access(normalized_path + i, os.R_OK):
                self.file_name_ = normalized_path + i
                logging.debug('WDB file found at %s', self.file_name_)

        if not self.file_name_:
            logging.error('No WDB file found based on "%s"', fname)
            sys.exit(1)

    def is_wch(self):
        return False

    def id_format(self):
        if self.id_format_str:
            return self.id_format_str

        # Adjust the size of the id formatter so we get consistent length
        # id field where we use it, and don't have to guess on the length
        # in the format file.
        n_digits = int(math.log10(self.last_id) + 1)
        self.id_format_str = '%%%uu' % n_digits
        return self.id_format_str

    # Sanitize data, blizzard started using dynamic width ints in WDB5, so
    # 3-byte ints have to be expanded to 4 bytes to parse them properly (with
    # struct module)
    def build_parser(self):
        format_str = '<'

        data_fmt = field_names = None
        if not self.options.raw:
            data_fmt = self.fmt.types(self.class_name())
            field_names = self.fmt.fields(self.class_name())
        field_idx = 0

        field_offset = 0
        for field_data_idx in range(0, len(self.field_data)):
            field_data = self.field_data[field_data_idx]
            type_idx = min(field_idx, data_fmt and len(data_fmt) - 1 or field_idx)
            field_size = field_data[1]
            for sub_idx in range(0, field_data[2]):
                if field_size == 1:
                    format_str += data_fmt and data_fmt[type_idx] or 'b'
                elif field_size == 2:
                    format_str += data_fmt and data_fmt[type_idx] or 'h'
                elif field_size >= 3:
                    format_str += data_fmt and data_fmt[type_idx].replace('S', 'I') or 'i'
                field_idx += 1

                if field_size == 3:
                    if not self.options.raw:
                        logging.debug('Unpacker has a 3-byte field (name=%s pos=%d): terminating (%s) and beginning a new unpacker',
                            field_names[type_idx], field_idx, format_str)
                    else:
                        logging.debug('Unpacker has a 3-byte field (pos=%d): terminating (%s) and beginning a new unpacker',
                            field_idx, format_str)
                    unpacker = struct.Struct(format_str)
                    self.unpackers.append((0xFFFFFF, unpacker, field_offset))
                    field_offset += unpacker.size - 1
                    format_str = '<'

        if len(format_str) > 1:
            self.unpackers.append((self.field_data[-1][1] == 3 and 0xFFFFFF or 0xFFFFFFFF, struct.Struct(format_str), field_offset))

        logging.debug('Unpacking plan for %s: %s',
            self.full_name(),
            ', '.join(['%s (len=%d, offset=%d)' % (u.format.decode('utf-8'), u.size, o) for _, u, o in self.unpackers]))
        if len(self.unpackers) == 1:
            self.record_parser = lambda ro, rs: self.unpackers[0][1].unpack_from(self.data, ro)
        else:
            self.record_parser = self.__do_parse

    def __do_parse(self, record_offset, record_size):
        full_data = []
        for mask, unpacker, offset in self.unpackers:
            full_data += unpacker.unpack_from(self.data, record_offset + offset)
            # TODO: Unsigned vs Signed
            full_data[-1] &= mask
        return full_data

    def n_expanded_fields(self):
        return sum([ fd[2] for fd in self.field_data ])

    # Can we search for data? This is only true, if we know where the ID in the data resides
    def searchable(self):
        return False

    def raw_outputtable(self):
        return False

    def full_name(self):
        return os.path.basename(self.file_name_)

    def file_name(self):
        return os.path.basename(self.file_name_).split('.')[0]

    def class_name(self):
        return os.path.basename(self.file_name_).split('.')[0]

    def name(self):
        return os.path.basename(self.file_name_).split('.')[0].replace('-', '_').lower()

    # Real record size is fields padded to powers of two
    def parsed_record_size(self):
        return self.record_size

    def n_records(self):
        return self.records

    def n_fields(self):
        return len(self.field_data)

    def n_records_left(self):
        return 0

    def parse_header(self):
        self.magic = self.data[:4]

        if not self.is_magic():
            logging.error('Invalid data file format %s', self.data[:4].decode('utf-8'))
            return False

        self.parse_offset += 4
        self.records, self.fields, self.record_size, self.string_block_size = _BASE_HEADER.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _BASE_HEADER.size

        return True

    def fields_str(self):
        fields = []

        fields.append('byte_size=%u' % len(self.data))
        fields.append('records=%u (%u)' % (self.records, self.n_records()))
        fields.append('fields=%u (%u)' % (self.fields, self.n_fields()))
        fields.append('o_data=%u' % self.data_offset)
        fields.append('record_size=%u' % self.record_size)
        if self.string_block_size > 0:
            fields.append('sb_size=%u' % self.string_block_size)
        if self.string_block_offset > 0:
            fields.append('o_sb=%u' % self.string_block_offset)

        return fields

    def __str__(self):
        return '%s::%s(%s)' % (self.full_name(), self.magic.decode('ascii'), ', '.join(self.fields_str()))

    def build_field_data(self):
        types = self.fmt.types(self.class_name())
        field_offset = 0
        self.field_data = []
        for t in types:
            if t in ['I', 'i', 'f', 'S']:
                self.field_data.append((field_offset, 4, 1))
            elif t in ['H', 'h']:
                self.field_data.append((field_offset, 2, 1))
            elif t in ['B', 'b']:
                self.field_data.append((field_offset, 1, 1))
            else:
                # If the format file has padding fields, add them as correct
                # length fields to the data format. These are only currently
                # used to align offset map files correctly, so data model
                # validation will work.
                m = re.match('([0-9]+)x', t)
                if m:
                    self.field_data.append((field_offset, int(m.group(1)), 1))

            field_offset += self.field_data[-1][1]

    def validate_data_model(self):
        unpacker = None
        # Sanity check the data
        try:
            unpacker = self.fmt.parser(self.class_name())
        except Exception as e:
            # We did not find a parser for the file, so then we need to figure
            # out if we can output it anyhow in some reduced form, since some
            # DBC versions are like that
            if not self.raw_outputtable():
                logging.error("Unable to parse %s: %s", self.full_name(), e)
                return False

        # Build auxilary structures to parse raw data out of the DBC file
        self.build_field_data()
        self.build_parser()

        if not unpacker:
            return True

        # Check that at the end of the day, we have a sensible record length
        if not self.raw_outputtable() and unpacker.size > self.parsed_record_size():
            logging.error("Record size mismatch for %s, expected %u, format has %u",
                    self.class_name(), self.record_size, unpacker.size)
            return False

        # Validate our json format file against the data we get from the file
        if not self.is_wch():
            str_base_incorrect = '[%03d/%03d] incorrect size for "%s" json-fmt="%s" dbc-length=%d json-length=%s file=%s'
            fields = self.fmt.fields(self.class_name())
            types = self.fmt.types(self.class_name())
            fidx = 0
            # Loop through all field data, validating each data type field (in
            # WDB4/5 header) against the json format's field type. String
            # fields are currently not validated as they can technically be of
            # any length.
            for fdidx in range(0, len(self.field_data)):
                fd = self.field_data[fdidx]
                for subidx in range(0, fd[2]):
                    # Since the WDB files may have padding, we may actually go
                    # past the number of json-format fields we have, so break
                    # out early in that case.
                    if fidx == len(fields):
                        break

                    if types[fidx] in ['I', 'i', 'f'] and fd[1] < 3:
                        logging.warn(str_base_incorrect, fdidx, fidx, fields[fidx], types[fidx], fd[1], '3+', self.full_name())
                    elif types[fidx] in ['H', 'h'] and fd[1] != 2:
                        logging.warn(str_base_incorrect, fdidx, fidx, fields[fidx], types[fidx], fd[1], '2', self.full_name())
                    elif types[fidx] in ['B', 'b'] and fd[1] != 1:
                        logging.warn(str_base_incorrect, fdidx, fidx, fields[fidx], types[fidx], fd[1], '1', self.full_name())
                    else:
                        logging.debug('[%03d/%03d] field length ok for "%s" json-fmt="%s" dbc-length=%d',
                            fdidx, fidx, fields[fidx], types[fidx], fd[1])
                    fidx += 1

        # Figure out the position of the id column. This may be none if WDB4/5
        # and id_block_offset > 0
        if not self.options.raw:
            fields = self.fmt.fields(self.class_name())
            for idx in range(0, len(fields)):
                if fields[idx] == 'id':
                    self.id_data = self.field_data[idx]
                    break

        return True

    def open(self):
        if self.data:
            return True

        f = io.open(self.file_name_, mode = 'rb')
        self.data = f.read()
        f.close()

        if not self.parse_header():
            return False

        if not self.validate_data_model():
            return False

        # After headers begins data, always
        self.data_offset = self.parse_offset

        # If this is an actual WDB file (or WCH file with -t view), setup the
        # correct id format to the formatter
        if not self.options.raw and (not self.is_wch() or self.options.type == 'view'):
            dbc.data._FORMATDB.set_id_format(self.class_name(), self.id_format())

        return True

    def get_string(self, offset):
        if offset == 0:
            return None

        end_offset = self.data.find(b'\x00', self.string_block_offset + offset)

        if end_offset == -1:
            return None

        return self.data[self.string_block_offset + offset:end_offset].decode('utf-8')

    def find_record_offset(self, id_):
        unpacker = None
        if self.id_data[1] == 1:
            unpacker = struct.Struct('B')
        elif self.id_data[1] == 2:
            unpacker = struct.Struct('H')
        elif self.id_data[1] >= 3:
            unpacker = struct.Struct('I')

        for record_id in range(0, self.n_records()):
            offset = self.data_offset + self.record_size * record_id + self.id_data[0]

            dbc_id = unpacker.unpack_from(self.data, offset)[0]
            # Hack to fix 3 byte fields, need to zero out the high byte
            if self.id_data[1] == 3:
                dbc_id &= 0x00FFFFFF

            if dbc_id == id_:
                return -1, self.data_offset + self.record_size * record_id, self.record_size

        return -1, 0, 0

    # Returns dbc_id (always 0 for base), record offset into file
    def get_record_info(self, record_id):
        return -1, self.data_offset + record_id * self.record_size, self.record_size

    def get_record(self, offset, size):
        return self.record_parser(offset, size)

    def find(self, id_):
        dbc_id, record_offset, record_size = self.find_record_offset(id_)

        if record_offset > 0:
            return dbc_id, self.record_parser(record_offset, record_size)
        else:
            return 0, tuple()

class InlineStringRecordParser:
    # Presume that string fields are always bunched up togeher
    def __init__(self, parser):
        self.parser = parser
        self.unpackers = []
        self.string_field_offset = 0
        self.n_string_fields = 0
        self.n_pad_fields = 0
        try:
            self.types = self.parser.fmt.types(self.parser.class_name())
            self.n_string_fields = sum([field == 'S' for field in self.types])
        except:
            logging.error('Inline-string based file %s requires formatting information', self.parser.class_name())
            sys.exit(1)

        data_fmt = field_names = None
        if not self.parser.options.raw:
            data_fmt = self.parser.fmt.types(self.parser.class_name())
            field_names = self.parser.fmt.fields(self.parser.class_name())
        # Need to build a two-split custom parser here
        format_str = '<'
        format_idx = 0
        n_bytes = 0
        for field_idx in range(0, len(self.parser.field_data)):
            # Don't parse past the record length
            if n_bytes == self.parser.record_size:
                break

            field_data = self.parser.field_data[field_idx]
            type_idx = min(format_idx, data_fmt and len(data_fmt) - 1 or format_idx)
            field_size = field_data[1]
            for sub_idx in range(0, field_data[2]):
                n_bytes += field_size
                # Don't parse past the record length
                if n_bytes == self.parser.record_size:
                    break

                if data_fmt and data_fmt[type_idx] == 'S':
                    logging.debug('Unpacker has a inlined string field (name=%s pos=%d): terminating (%s), skipping all consecutive strings, and beginning a new unpacker',
                        field_names[type_idx], field_idx, format_str)
                    self.unpackers.append((False, struct.Struct(format_str)))
                    self.string_field_offset = self.unpackers[-1][1].size
                    format_str = '<'
                    format_idx += 1

                    while data_fmt[type_idx + 1] == 'S':
                        format_idx += 1
                        type_idx += 1
                        field_idx += 1
                    continue

                # Padding fields need to be skipped here, and filled with dummy
                # data on parsing. This way, our data model will not break on
                # outputting (# of fields, # of data will stay consistent)
                if data_fmt and 'x' in data_fmt[type_idx]:
                    logging.debug('Skipping padding field (name=%s pos=%d) of type "%s"', field_names[type_idx], field_idx, data_fmt[type_idx])
                    format_idx += 1
                    type_idx += 1
                    field_idx += 1
                    self.n_pad_fields += 1
                    continue

                if field_size == 1:
                    format_str += data_fmt and data_fmt[type_idx] or 'b'
                elif field_size == 2:
                    format_str += data_fmt and data_fmt[type_idx] or 'h'
                elif field_size >= 3:
                    format_str += data_fmt and data_fmt[type_idx].replace('S', 'I') or 'i'
                format_idx += 1

                if field_size == 3:
                    if not self.options.raw:
                        logging.debug('Unpacker has a 3-byte field (name=%s pos=%d): terminating (%s) and beginning a new unpacker',
                            field_names[type_idx], field_idx, format_str)
                    else:
                        logging.debug('Unpacker has a 3-byte field (pos=%d): terminating (%s) and beginning a new unpacker',
                            field_idx, format_str)
                    unpackers.append((True, struct.Struct(format_str)))
                    format_str = '<'

        if len(format_str) > 1:
            self.unpackers.append((self.parser.field_data[-1][1] == 3, struct.Struct(format_str)))

        logging.debug('Unpacking plan: %s', ', '.join(['%s (len=%d)' % (u.format.decode('utf-8'), u.size) for _, u in self.unpackers]))

    def __call__(self, offset, size):
        full_data = []
        field_offset = 0
        parsed_string_fields = 0

        for int24, unpacker in self.unpackers:
            full_data += unpacker.unpack_from(self.parser.data, offset + field_offset)
            field_offset += unpacker.size

            # String fields begin
            if field_offset == self.string_field_offset:
                while parsed_string_fields < self.n_string_fields:
                    if self.parser.data[offset + field_offset] != 0:
                        end = self.parser.data.find(b'\x00', offset + field_offset, offset + size)
                        full_data.append(offset + field_offset)
                        if self.parser.data[end + 4] == 0:
                            field_offset = end - offset + 1
                        else:
                            field_offset = end - offset + 4
                    else:
                        full_data.append(0)
                        field_offset += 4

                    parsed_string_fields += 1

                # Append pad fields with zeros to fill up to correct length
                full_data += [ 0, ] * self.n_pad_fields

            if int24:
                full_data[-1] &= 0xFFFFFF

        return full_data

class LegionWDBParser(DBCParserBase):
    def __init__(self, options, fname):
        super().__init__(options, fname)

        self.id_block_offset = 0
        self.clone_block_offset = 0
        self.offset_map_offset = 0

        self.id_table = []

    def has_offset_map(self):
        return self.flags & X_OFFSET_MAP

    def has_id_block(self):
        return self.flags & X_ID_BLOCK

    def n_cloned_records(self):
        return self.clone_segment_size // _CLONE.size

    def n_records(self):
        if self.id_block_offset:
            return len(self.id_table)
        else:
            return self.records

    # Inline strings need some (very heavy) custom parsing
    def build_parser(self):
        if self.has_offset_map():
            self.record_parser = InlineStringRecordParser(self)
        else:
            super().build_parser()

    # Can search through WDB4/5 files if there's an id block, or if we can find
    # an ID from the formatted data
    def searchable(self):
        if self.options.raw:
            return self.id_block_offset > 0
        else:
            return True

    def find_record_offset(self, id_):
        if self.has_id_block():
            for record_id in range(0, self.n_records()):
                if self.id_table[record_id][0] == id_:
                    return self.id_table[record_id]
            return -1, 0, 0
        else:
            return super().find_record_offset(id_)

    def offset_map_entry(self, offset):
        return _ITEMRECORD.unpack_from(self.data, offset)

    def offset_map_entry_size(self):
        return _ITEMRECORD.size

    def build_id_table(self):
        if self.id_block_offset == 0:
            return

        idtable = []
        indexdict = {}

        # Process ID block
        unpacker = struct.Struct('%dI' % self.records)
        record_id = 0
        for dbc_id in unpacker.unpack_from(self.data, self.id_block_offset):
            data_offset = 0
            size = self.record_size

            # If there is an offset map, the correct data offset and record
            # size needs to be fetched from the offset map. The offset map
            # contains sparse entries (i.e., it has last_id-first_id entries,
            # some of which are zeros if there is no dbc id for a given item.
            if self.has_offset_map():
                record_index = dbc_id - self.first_id
                record_data_offset = self.offset_map_offset + record_index * self.offset_map_entry_size()
                data_offset, size = self.offset_map_entry(record_data_offset)
            else:
                data_offset = self.data_offset + record_id * self.record_size

            idtable.append((dbc_id, data_offset, size))
            indexdict[dbc_id] = (data_offset, size)
            record_id += 1

        # Process clones
        for clone_id in range(0, self.n_cloned_records()):
            clone_offset = self.clone_block_offset + clone_id * _CLONE.size
            target_id, source_id = _CLONE.unpack_from(self.data, clone_offset)
            if source_id not in indexdict:
                continue

            idtable.append((target_id, indexdict[source_id][0], indexdict[source_id][1]))

        self.id_table = idtable
        # If we have an idtable, just index directly to it
        self.get_record_info = lambda record_id: self.id_table[record_id]

    def open(self):
        if not super().open():
            return False

        self.build_id_table()

        logging.debug('Opened %s' % self.full_name())
        return True

    def fields_str(self):
        fields = super().fields_str()

        fields.append('table_hash=%#.8x' % self.table_hash)
        if hasattr(self, 'build'):
            fields.append('build=%u' % self.build)
        if hasattr(self, 'timestamp'):
            fields.append('timestamp=%u' % self.timestamp)
        fields.append('first_id=%u' % self.first_id)
        fields.append('last_id=%u' % self.last_id)
        fields.append('locale=%#.8x' % self.locale)
        if self.clone_segment_size > 0:
            fields.append('clone_size=%u' % self.clone_segment_size)
            fields.append('o_clone_block=%u' % self.clone_block_offset)
        if self.id_block_offset > 0:
            fields.append('o_id_block=%u' % self.id_block_offset)
        if self.offset_map_offset > 0:
            fields.append('o_offset_map=%u' % self.offset_map_offset)
        if hasattr(self, 'flags'):
            fields.append('flags=%#.8x' % self.flags)

        return fields

class WDB4Parser(LegionWDBParser):
    def is_magic(self): return self.magic == b'WDB4'

    # TODO: Move this to a defined set of (field, type, attribute) triples in DBCParser
    def parse_header(self):
        if not super().parse_header():
            return False

        self.table_hash, self.build, self.timestamp = _DB_HEADER_1.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _DB_HEADER_1.size

        self.first_id, self.last_id, self.locale, self.clone_segment_size = _DB_HEADER_2.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _DB_HEADER_2.size

        self.flags = struct.unpack_from('I', self.data, self.parse_offset)[0]
        self.parse_offset += 4

        # Setup offsets into file, first string block
        if self.string_block_size > 2:
            self.string_block_offset = self.parse_offset + self.records * self.record_size

        # Has ID block
        if self.has_id_block():
            self.id_block_offset = self.parse_offset + self.records * self.record_size + self.string_block_size

        # Offset map contains offset into file, record size entries in a sparse structure
        if self.has_offset_map():
            self.offset_map_offset = self.string_block_size
            self.string_block_offset = 0
            if self.has_id_block():
                self.id_block_offset = self.string_block_size + ((self.last_id - self.first_id) + 1) * _ITEMRECORD.size

        # Has clone block
        if self.clone_segment_size > 0:
            self.clone_block_offset = self.id_block_offset + self.records * _ID.size

        return True

class WDB5Parser(LegionWDBParser):
    def is_magic(self): return self.magic == b'WDB5'

    # Parses header field information to field_data. Note that the tail end has
    # to be guessed because we don't know whether there's padding in the file
    # or whether the last field is an array.
    def build_field_data(self):
        prev_field = None
        for field_idx in range(0, self.fields):
            raw_size, record_offset = _FIELD_DATA.unpack_from(self.data, self.parse_offset)
            type_size = (32 - raw_size) // 8

            self.parse_offset += _FIELD_DATA.size
            distance = 0
            if prev_field:
                distance = record_offset - prev_field[0]
                n_fields = distance // prev_field[1]
                self.field_data[-1][2] = n_fields

            self.field_data.append([record_offset, type_size, 0])

            prev_field = (record_offset, type_size)

        bytes_left = self.record_size - self.field_data[-1][0]
        while bytes_left >= self.field_data[-1][1]:
            self.field_data[-1][2] += 1
            bytes_left -= self.field_data[-1][1]

        logging.debug('Generated field data for %d fields (header is %d)', len(self.field_data), self.fields)

        return True

    # WDB5 allows us to build an automatic decoder for the data, because the
    # field widths are included in the header. The decoder cannot distinguish
    # between signed/unsigned nor floats, but it will still give same data for
    # the same byte values
    def build_decoder(self):
        fields = ['=',]
        for field_data in self.field_data:
            if field_data[1] > 2:
                fields.append('i')
            elif field_data[1] == 2:
                fields.append('h')
            elif field_data[1] == 1:
                fields.append('b')
        return struct.Struct(''.join(fields))

    # TODO: Move this to a defined set of (field, type, attribute) triples in DBCParser
    def parse_header(self):
        if not super().parse_header():
            return False

        self.table_hash, self.layout_hash, self.first_id = _DB_HEADER_1.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _DB_HEADER_1.size

        self.last_id, self.locale, self.clone_segment_size, self.flags, self.id_index = _DB_HEADER_2.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _DB_HEADER_2.size

        # Setup offsets into file, first string block, skip field data information of WDB5 files
        if self.string_block_size > 2:
            self.string_block_offset = self.parse_offset + self.records * self.record_size
            self.string_block_offset += self.fields * 4

        # Has ID block, note that ID-block needs to skip the field data information on WDB5 files
        if self.has_id_block():
            self.id_block_offset = self.parse_offset + self.records * self.record_size + self.string_block_size
            self.id_block_offset += self.fields * 4

        # Offset map contains offset into file, record size entries in a sparse structure
        if self.has_offset_map():
            self.offset_map_offset = self.string_block_size
            self.string_block_offset = 0
            if self.has_id_block():
                self.id_block_offset = self.string_block_size + ((self.last_id - self.first_id) + 1) * _ITEMRECORD.size

        # Has clone block
        if self.clone_segment_size > 0:
            self.clone_block_offset = self.id_block_offset + self.records * _ID.size

        return True

    def fields_str(self):
        fields = super().fields_str()

        fields.append('layout_hash=%#.8x' % self.layout_hash)
        if self.id_index > 0:
            fields.append('id_index=%u' % self.id_index)

        return fields

    # WDB5 can always output some human readable data, even if the field types
    # are not correct, but only do this if --raw is enabled
    def raw_outputtable(self):
        return self.options.raw

    def n_fields(self):
        return sum([fd[2] for fd in self.field_data])

    # This is the padded record size, meaning 3 byte fields are padded to 4 bytes
    def parsed_record_size(self):
        return sum([(fd[1] == 3 and 4 or fd[1]) * fd[2] for fd in self.field_data])

    def validate_data_model(self):
        if not super().validate_data_model():
            return False

        return True

    def __str__(self):
        s = super().__str__()

        fields = []
        for i in range(0, len(self.field_data)):
            field_data = self.field_data[i]
            fields.append('#%d: %s%s@%d' % (
                len(fields) + 1, 'int%d' % (field_data[1] * 8), field_data[2] > 1 and ('[%d]' % field_data[2]) or '',
                field_data[0]
            ))

        if len(self.field_data):
            s += '\nField data: %s' % ', '.join(fields)

        return s

class LegionWCHParser(LegionWDBParser):
    # For some peculiar reason, some WCH files are completely alien, compared
    # to the rest, and expand all the dynamic width variables to 4 byte fields.
    # Manually make a list here to support those files in build_parser.
    __override_dbcs__ = [ 'SpellEffect' ]

    def is_magic(self): return self.magic == b'WCH5' or self.magic == b'WCH6'

    def __init__(self, options, wdb_parser, fname):
        super().__init__(options, fname)

        self.clone_segment_size = 0 # WCH files never have a clone segment
        self.wdb_parser = wdb_parser

    def has_id_block(self):
        return self.wdb_parser.has_id_block()

    def has_offset_map(self):
        return self.wdb_parser.has_offset_map()

    def parse_header(self):
        if not super().parse_header():
            return False

        self.table_hash, self.layout_hash, self.build, self.timestamp, self.first_id, self.last_id, self.locale = _WCH5_HEADER.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _WCH5_HEADER.size

        # Setup offsets into file, first string block
        if self.string_block_size > 2:
            self.string_block_offset = self.parse_offset + self.records * self.record_size

        # Offset map contains offset into file, record size entries in a sparse structure
        if self.has_offset_map():
            self.offset_map_offset = self.parse_offset
            self.string_block_offset = 0
            self.id_block = 0

        # Has ID block
        if self.has_id_block():
            self.id_block_offset = self.parse_offset + self.records * self.record_size + self.string_block_size

        return True

    def build_id_table(self):
        if self.id_block_offset == 0 and self.offset_map_offset == 0:
            return

        record_id = 0
        if self.has_offset_map():
            while record_id < self.records:
                ofs_offset_map_entry = self.offset_map_offset + record_id * _WCH_ITEMRECORD.size
                dbc_id, data_offset, size = _WCH_ITEMRECORD.unpack_from(self.data, ofs_offset_map_entry)
                self.id_table.append((dbc_id, data_offset, size))
                record_id += 1
        elif self.has_id_block():
            unpacker = struct.Struct('%dI' % self.records)
            for dbc_id in unpacker.unpack_from(self.data, self.id_block_offset):
                size = self.record_size
                data_offset = self.data_offset + record_id * self.record_size
                self.id_table.append((dbc_id, data_offset, size))
                record_id += 1

        # If we have an idtable, just index directly to it
        self.get_record_info = lambda record_id: self.id_table[record_id]

    def is_wch(self):
        return True

    # WCH files may need a specialized parser building if the parent WDB file
    # does not use an ID block. If ID block is used, the corresponding WCH file
    # will also have dynamic width fields. As an interesting side note, the WCF
    # files actually have record sizes at the correct length, and not padded.
    def build_parser(self):
        if self.class_name() in self.__override_dbcs__:
            logging.debug('==NOTE== Overridden DBC: Expanding all record fields to 4 bytes ==NOTE==')
            self.build_parser_wch5()
        else:
            super().build_parser()

    # Override record parsing completely by making a unpacker that generates 4
    # byte fields for the record. This is necessary in the case of SpellEffect
    # (as of 2016/7/17 at least), since the actual client data file format is
    # not honored at all. There does not seem to be an identifying marker in
    # the client data (or the cache file) to automatically determine when to
    # use the overridden builder, so __override_dbcs__ contains a list of files
    # where it is used.
    def build_parser_wch5(self):
        format_str = '<'

        data_fmt = field_names = None
        if not self.options.raw:
            data_fmt = self.fmt.types(self.class_name())
        field_idx = 0

        field_offset = 0
        for field_data_idx in range(0, len(self.field_data)):
            field_data = self.field_data[field_data_idx]
            type_idx = min(field_idx, data_fmt and len(data_fmt) - 1 or field_idx)
            for sub_idx in range(0, field_data[2]):
                if data_fmt[type_idx] in ['S', 'H', 'B']:
                    format_str += 'I'
                elif data_fmt[type_idx] in ['h', 'b']:
                    format_str += 'i'
                else:
                    format_str += data_fmt[type_idx]
                field_idx += 1

        if len(format_str) > 1:
            self.unpackers.append((0xFFFFFFFF, struct.Struct(format_str), field_offset))

        logging.debug('Unpacking plan for %s: %s',
            self.full_name(),
            ', '.join(['%s (len=%d, offset=%d)' % (u.format.decode('utf-8'), u.size, o) for _, u, o in self.unpackers]))
        if len(self.unpackers) == 1:
            self.record_parser = lambda ro, rs: self.unpackers[0][1].unpack_from(self.data, ro)
        else:
            self.record_parser = self.__do_parse

class WCH7Parser(LegionWCHParser):
    def is_magic(self): return self.magic == b'WCH7'

    def __init__(self, options, wdb_parser, fname):
        super().__init__(options, wdb_parser, fname)

    # Completely rewrite WCH7 parser, since the base header gained a new field
    def parse_header(self):
        self.magic = self.data[:4]

        if not self.is_magic():
            logging.error('Invalid data file format %s', self.data[:4].decode('utf-8'))
            return False

        self.parse_offset += 4
        self.records, self.wch7_unk, self.fields, self.record_size, self.string_block_size = _WCH7_BASE_HEADER.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _WCH7_BASE_HEADER.size

        self.table_hash, self.layout_hash, self.build, self.timestamp, self.first_id, self.last_id, self.locale = _WCH5_HEADER.unpack_from(self.data, self.parse_offset)
        self.parse_offset += _WCH5_HEADER.size

        # Setup offsets into file, first string block
        if self.string_block_size > 2:
            self.string_block_offset = self.parse_offset + self.records * self.record_size

        # Offset map contains offset into file, record size entries in a sparse structure
        if self.has_offset_map():
            self.offset_map_offset = self.parse_offset
            self.string_block_offset = 0
            self.id_block = 0

        # Has ID block
        if self.has_id_block():
            self.id_block_offset = self.parse_offset + self.records * self.record_size + self.string_block_size

        return True

    def fields_str(self):
        fields = super().fields_str()

        fields.append('unk_wch7=%u' % self.wch7_unk)

        return fields
