# coding: utf8
r"""
    weasyprint.pdf
    --------------

    Post-process the PDF files created by cairo and add metadata such as
    hyperlinks and bookmarks.


    Rather than trying to parse any valid PDF, we make some assumptions
    that hold for cairo in order to simplify the code:

    * All newlines are '\n', not '\r' or '\r\n'
    * Except for number 0 (which is always free) there is no "free" object.
    * Most white space separators are made of a single 0x20 space.
    * Indirect dictionary objects do not contain '>>' at the start of a line
      except to mark the end of the object, followed by 'endobj'.
      (In other words, '>>' markers for sub-dictionaries are indented.)
    * The Page Tree is flat: all kids of the root page node are page objects,
      not page tree nodes.

    However the code uses a lot of assert statements so that if an assumptions
    is not true anymore, the code should (hopefully) fail with an exception
    rather than silently behave incorrectly.


    :copyright: Copyright 2011-2012 Simon Sapin and contributors, see AUTHORS.
    :license: BSD, see LICENSE for details.

"""

from __future__ import division, unicode_literals

import os
import re
import string

import cairo

from . import VERSION_STRING
from .logger import LOGGER
from .compat import xrange, iteritems
from .urls import iri_to_uri
from .formatting_structure import boxes
from .css.computed_values import LENGTHS_TO_PIXELS


PX_TO_PT = 1 / LENGTHS_TO_PIXELS['pt']


class PDFFormatter(string.Formatter):
    """Like str.format except:

    * Results are byte strings
    * The new !P conversion flags encodes a PDF string.
      (UTF-16 BE with a BOM, then backslash-escape parentheses.)

    Except for fields marked !P, everything should be ASCII-only.

    """
    def convert_field(self, value, conversion):
        if conversion == 'P':
            # Make a round-trip back through Unicode for the .translate()
            # method. (bytes.translate only maps to single bytes.)
            # Use latin1 to map all byte values.
            return '({0})'.format(
                ('\ufeff' + value).encode('utf-16-be').decode('latin1')
                .translate({40: r'\(', 41: r'\)', 92: r'\\'}))
        else:
            return super(PDFFormatter, self).convert_field(value, conversion)

    def vformat(self, format_string, args, kwargs):
        result = super(PDFFormatter, self).vformat(format_string, args, kwargs)
        return result.encode('latin1')

pdf_format = PDFFormatter().format


class PDFDictionary(object):
    def __init__(self, object_number, byte_string):
        self.object_number = object_number
        self.byte_string = byte_string

    def __repr__(self):
        return self.__class__.__name__ + repr(
            (self.object_number, self.byte_string))

    _re_cache = {}

    def get_value(self, key, value_re):
        regex = self._re_cache.get((key, value_re))
        if not regex:
            regex = re.compile(pdf_format('/{0} {1}', key, value_re))
            self._re_cache[key, value_re] = regex
        return regex.search(self.byte_string).group(1)

    def get_type(self):
        """
        :returns: the value for the /Type key.

        """
        # No end delimiter, + defaults to greedy
        return self.get_value('Type', '/(\w+)').decode('ascii')

    def get_indirect_dict(self, key, pdf_file):
        """Read the value for `key` and follow the reference, assuming
        it is an indirect dictionary object.

        :return: a new PDFDictionary instance.

        """
        object_number = int(self.get_value(key, '(\d+) 0 R'))
        return type(self)(object_number, pdf_file.read_object(object_number))

    def get_indirect_dict_array(self, key, pdf_file):
        """Read the value for `key` and follow the references, assuming
        it is an array of indirect dictionary objects.

        :return: a list of new PDFDictionary instance.

        """
        parts = self.get_value(key, '\[(.+?)\]').split(b' 0 R')
        # The array looks like this: ' <a> 0 R <b> 0 R <c> 0 R '
        # so `parts` ends up like this [' <a>', ' <b>', ' <c>', ' ']
        # With the trailing white space in the list.
        trail = parts.pop()
        assert not trail.strip()
        class_ = type(self)
        read = pdf_file.read_object
        return [class_(n, read(n)) for n in map(int, parts)]


class PDFFile(object):
    """
    :param fileobj:
        A seekable binary file-like object for a PDF generated by cairo.

    """
    trailer_re = re.compile(
        b'\ntrailer\n(.+)\nstartxref\n(\d+)\n%%EOF\n$', re.DOTALL)

    def __init__(self, fileobj):
        # cairo’s trailer only has Size, Root and Info.
        # The trailer + startxref + EOF is typically under 100 bytes
        fileobj.seek(-200, os.SEEK_END)
        trailer, startxref = self.trailer_re.search(fileobj.read()).groups()
        trailer = PDFDictionary(None, trailer)
        startxref = int(startxref)

        fileobj.seek(startxref)
        line = next(fileobj)
        assert line == b'xref\n'

        line = next(fileobj)
        first_object, total_objects = line.split()
        assert first_object == b'0'
        total_objects = int(total_objects)

        line = next(fileobj)
        assert line == b'0000000000 65535 f \n'

        objects_offsets = [None]
        for object_number in xrange(1, total_objects):
            line = next(fileobj)
            assert line[10:] == b' 00000 n \n'
            objects_offsets.append(int(line[:10]))

        self.fileobj = fileobj
        #: Maps object number -> bytes from the start of the file
        self.objects_offsets = objects_offsets

        info = trailer.get_indirect_dict('Info', self)
        catalog = trailer.get_indirect_dict('Root', self)
        page_tree = catalog.get_indirect_dict('Pages', self)
        pages = page_tree.get_indirect_dict_array('Kids', self)
        # Check that the tree is flat
        assert all(p.get_type() == 'Page' for p in pages)

        self.startxref = startxref
        self.info = info
        self.catalog = catalog
        self.page_tree = page_tree
        self.pages = pages

        self.finished = False
        self.overwritten_objects_offsets = {}
        self.new_objects_offsets = []

    def read_object(self, object_number):
        """
        :param object_number:
            An integer N so that 1 <= N < len(self.objects_offsets)
        :returns:
            The object content as a byte string.

        """
        fileobj = self.fileobj
        fileobj.seek(self.objects_offsets[object_number])
        line = next(fileobj)
        assert line.endswith(b' 0 obj\n')
        assert int(line[:-7]) == object_number  # len(b' 0 obj\n') == 7
        object_lines = []
        for line in fileobj:
            object_lines.append(line)
            if line == b'>>\n':
                assert next(fileobj) == b'endobj\n'
                return b''.join(object_lines)

    def overwrite_object(self, object_number, byte_string):
        """Write the new content for an existing object at the end of the file.

        :param object_number:
            An integer N so that 1 <= N < len(self.objects_offsets)
        :param byte_string:
            The new object content as a byte string.

        """
        self.overwritten_objects_offsets[object_number] = (
            self._write_object(object_number, byte_string))

    def extend_dict(self, dictionary, new_content):
        """Overwrite a dictionary object after adding content inside
        the << >> delimiters.

        """
        assert dictionary.byte_string.endswith(b'>>\n')
        self.overwrite_object(
            dictionary.object_number,
            dictionary.byte_string[:-3] + new_content + b'\n>>\n')

    def next_object_number(self):
        """Return the object number that would be used by write_new_object().
        """
        return len(self.objects_offsets) + len(self.new_objects_offsets)

    def write_new_object(self, byte_string):
        """Write a new object at the end of the file.

        :param byte_string:
            The object content as a byte string.
        :return:
            The new object number.

        """
        object_number = self.next_object_number()
        self.new_objects_offsets.append(
            self._write_object(object_number, byte_string))
        return object_number

    def finish(self):
        """
        Write the cross-reference table and the trailer for the new and
        overwritten objects. This makes `fileobj` a valid (updated) PDF file.

        """
        new_startxref, write = self._start_writing()
        self.finished = True
        write(b'xref\n')

        # Don’t bother sorting or finding contiguous numbers,
        # just write a new sub-section for each overwritten object.
        for object_number, offset in iteritems(
                self.overwritten_objects_offsets):
            write(pdf_format(
                '{0} 1\n{1:010} 00000 n \n', object_number, offset))

        if self.new_objects_offsets:
            first_new_object = len(self.objects_offsets)
            write(pdf_format(
                '{0} {1}\n', first_new_object, len(self.new_objects_offsets)))
            for object_number, offset in enumerate(
                    self.new_objects_offsets, start=first_new_object):
                write(pdf_format('{0:010} 00000 n \n', offset))

        write(pdf_format(
            'trailer\n<< '
            '/Size {size} /Root {root} 0 R /Info {info} 0 R /Prev {prev}'
            ' >>\nstartxref\n{startxref}\n%%EOF\n',
            size=self.next_object_number(),
            root=self.catalog.object_number,
            info=self.info.object_number,
            prev=self.startxref,
            startxref=new_startxref))

    def _write_object(self, object_number, byte_string):
        offset, write = self._start_writing()
        write(pdf_format('{0} 0 obj\n', object_number))
        write(byte_string)
        write(b'\nendobj\n')
        return offset

    def _start_writing(self):
        assert not self.finished
        fileobj = self.fileobj
        fileobj.seek(0, os.SEEK_END)
        return fileobj.tell(), fileobj.write


def process_bookmarks(raw_bookmarks):
    """Transform a list of bookmarks as found in the document
    to a data structure ready for PDF.

    """
    root = {'Count': 0}
    bookmark_list = []

    level_shifts = []
    last_by_level = [root]
    indices_by_level = [0]

    for i, (level, label, destination) in enumerate(raw_bookmarks, start=1):
        # Calculate the real level of the bookmark
        previous_level = len(last_by_level) - 1 + sum(level_shifts)
        if level > previous_level:
            level_shifts.append(level - previous_level - 1)
        else:
            k = 0
            while k < previous_level - level:
                k += 1 + level_shifts.pop()

        # Resolve level inconsistencies
        level -= sum(level_shifts)

        bookmark = {
            'Count': 0, 'First': None, 'Last': None, 'Prev': None,
            'Next': None, 'Parent': indices_by_level[level - 1],
            'label': label, 'destination': destination}

        if level > len(last_by_level) - 1:
            last_by_level[level - 1]['First'] = i
        else:
            # The bookmark is sibling of indices_by_level[level]
            bookmark['Prev'] = indices_by_level[level]
            last_by_level[level]['Next'] = i

            # Remove the bookmarks with a level higher than the current one
            del last_by_level[level:]
            del indices_by_level[level:]

        for count_level in range(level):
            last_by_level[count_level]['Count'] += 1
        last_by_level[level - 1]['Last'] = i

        last_by_level.append(bookmark)
        indices_by_level.append(i)
        bookmark_list.append(bookmark)

    return root, bookmark_list


def gather_metadata(document):
    """Traverse the layout tree (boxes) to find all metadata."""
    def walk(box):
        # "Border area. That's the area that hit-testing is done on."
        # http://lists.w3.org/Archives/Public/www-style/2012Jun/0318.html
        if box.bookmark_label and box.bookmark_level:
            pos_x, pos_y = point_to_pdf(box.border_box_x(), box.border_box_y())
            bookmarks.append((
                box.bookmark_level,
                box.bookmark_label,
                (page_index, pos_x, pos_y)))

        if box.style.link:
            pos_x, pos_y = point_to_pdf(box.border_box_x(), box.border_box_y())
            width, height = distance_to_pdf(
                box.border_width(), box.border_height())
            page_links.append(
                (box, (pos_x, pos_y, pos_x + width, pos_y + height)))

        if box.style.anchor and box.style.anchor not in anchors:
            pos_x, pos_y = point_to_pdf(box.border_box_x(), box.border_box_y())
            anchors[box.style.anchor] = (page_index, pos_x, pos_y)

        if isinstance(box, boxes.ParentBox):
            for child in box.children:
                walk(child)

    bookmarks = []
    links_by_page = []
    anchors = {}
    for page_index, page in enumerate(document.pages):
        # cairo coordinates are pixels right and down from the top-left corner
        # PDF coordinates are points right and up from the bottom-left corner
        matrix = cairo.Matrix(
            PX_TO_PT, 0, 0, -PX_TO_PT, 0, page.outer_height * PX_TO_PT)
        point_to_pdf = matrix.transform_point
        distance_to_pdf = matrix.transform_distance
        page_links = []
        walk(page)
        links_by_page.append(page_links)

    # A list (by page) of lists of either:
    # ('external', uri, rectangle) or
    # ('internal', (page_index, target_x, target_y), rectangle)
    resolved_links_by_page = []
    for page_links in links_by_page:
        resolved_page_links = []
        for box, rectangle in page_links:
            type_, href = box.style.link
            if type_ == 'internal':
                target = anchors.get(href)
                if target is None:
                    LOGGER.warn(
                        'No anchor #%s for internal URI reference at line %s'
                        % (href, box.sourceline))
                else:
                    resolved_page_links.append((type_, target, rectangle))
            else:
                # external link:
                resolved_page_links.append((type_, href, rectangle))
        resolved_links_by_page.append(resolved_page_links)

    return process_bookmarks(bookmarks), resolved_links_by_page


def write_pdf_metadata(document, fileobj):
    bookmarks, links = gather_metadata(document)

    pdf = PDFFile(fileobj)
    pdf.overwrite_object(pdf.info.object_number, pdf_format(
        '<< /Producer {producer!P} >>',
        producer=VERSION_STRING))

    root, bookmarks = bookmarks
    if bookmarks:
        bookmark_root = pdf.next_object_number()
        pdf.write_new_object(pdf_format(
            '<< /Type /Outlines /Count {0} /First {1} 0 R /Last {2} 0 R\n>>',
            root['Count'],
            root['First'] + bookmark_root,
            root['Last'] + bookmark_root))
        pdf.extend_dict(pdf.catalog, pdf_format(
            '/Outlines {0} 0 R /PageMode /UseOutlines', bookmark_root))
        for bookmark in bookmarks:
            content = [pdf_format('<< /Title {0!P}\n', bookmark['label'])]
            if bookmark['Count']:
                content.append(pdf_format('/Count {0}\n', bookmark['Count']))
            for key in ['Parent', 'Prev', 'Next', 'First', 'Last']:
                if bookmark[key]:
                    content.append(pdf_format(
                        '/{0} {1} 0 R\n', key, bookmark[key] + bookmark_root))
            content.append(pdf_format(
                '/A << /Type /Action /S /GoTo '
                    '/D [{0} /XYZ {1:f} {2:f} 0] >>\n>>',
                *bookmark['destination']))
            pdf.write_new_object(b''.join(content))

    for page, page_links in zip(pdf.pages, links):
        annotations = []
        for is_internal, target, rectangle in page_links:
            content = [pdf_format(
                '<< /Type /Annot /Subtype /Link '
                    '/Rect [{0:f} {1:f} {2:f} {3:f}] /Border [0 0 0]\n',
                *rectangle)]
            if is_internal == 'internal':
                content.append(pdf_format(
                    '/A << /Type /Action /S /GoTo '
                        '/D [{0} /XYZ {1:f} {2:f} 0] >>\n',
                    *target))
            else:
                content.append(pdf_format(
                    '/A << /Type /Action /S /URI /URI ({0}) >>\n',
                    iri_to_uri(target)))
            content.append(b'>>')
            annotations.append(pdf.write_new_object(b''.join(content)))

        if annotations:
            pdf.extend_dict(page, pdf_format(
                '/Annots [{0}]', ' '.join(
                    '{0} 0 R'.format(n) for n in annotations)))

    pdf.finish()
