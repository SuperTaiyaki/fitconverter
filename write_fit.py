#!/usr/bin/python
# vim: et sw=4 ts=4

import sys
import struct
import datetime
import time
import binascii
import math
from xml.dom.minidom import parse
import xml.etree.ElementTree as ET

# Garmin-defined
epoch = datetime.datetime(1989, 12, 31, 0, 0, 0)

track_name = ""
laps = []
trackpoints = []

# FIT lat/lon units
def degree_to_semicircle(degree):
    return int(degree * (2**31 / 180))

# GPX samples don't have distance, but FIT requires it. Simple estimation of distance
# Could be replaced with Haversine, but probably won't make much difference.
# p1, p2 are (lat, lon) in degrees
# Result is in cm
def distance_ll(p1, p2):
    p1 = [x*math.pi/180.0 for x in p1]
    p2 = [x*math.pi/180.0 for x in p2]
    x = (p2[1]-p1[1]) * math.cos((p1[0]+p1[1])/2);
    y = (p2[0] - p1[0]);
    return int(math.hypot(x, y) * 6.3675e8); # Radius of the earth in cm


def step_tcx(data):
    partial_laps = []
    # Lap start _time_ needs to be logged... same as the timestamp?
    def track_point(node):
        time_raw = node.find("Time").text
        # Garmin Connect  includes .%f, RideWithGPS doesn't.
        if '.' in time_raw:
            format = "%Y-%m-%dT%H:%M:%S.%fZ"
        else:
            format = "%Y-%m-%dT%H:%M:%S"
        time = datetime.datetime.strptime(time_raw, format)
        etime = time - epoch
        distance = int(float(node.find("DistanceMeters").text)*100)
        # Should probably XPath this
        lat = float(node.find("./Position/LatitudeDegrees").text)
        lon = float(node.find("./Position/LongitudeDegrees").text)
        return (int(etime.total_seconds()), degree_to_semicircle(lat),
                degree_to_semicircle(lon), distance)

    for element in data.findall('Courses'):
        course = element.find('Course')
        global track_name
        track_name = course.find("Name").text
        for lap in course.findall("Lap"):
            time = 0.0
            distance = 0
            start = (0.0, 0.0)
            time = float(lap.find("TotalTimeSeconds").text)
            distance = int(float(lap.find("DistanceMeters").text)*100) # in cm

            lat = float(lap.find("BeginPosition/LatitudeDegrees").text)
            lon = float(lap.find("BeginPosition/LongitudeDegrees").text)
            start = (degree_to_semicircle(lat), degree_to_semicircle(lon))

            partial_laps.append((time, distance, start))

        for tp in course.findall("Track/Trackpoint"):
            trackpoints.append(track_point(tp))

    # Crunch the lap data - map each point to the first track point that matches in order to steal the timestamp
    for lap in partial_laps:
        for point in trackpoints:
            if lap[2][0] == point[1] and lap[2][1] == point[2]:
                laps.append((0, point[0]))
                break

def step_gpx(data):
    global track_name
    track_name = data.find("trk/name").text + "\0"

    last_point = None
    time = 0 # Arbitrary... just fill it in randomly
    for node in data.findall("trk/trkseg/trkpt"):

        lat = float(node.get('lat'))
        lon = float(node.get('lon'))
        if last_point:
            distance = distance_ll((lat, lon), last_point)
        else:
            distance = 0
        # Write out point
        trackpoints.append((time, degree_to_semicircle(lat), degree_to_semicircle(lon), distance))
        time += 100
        last_point = (lat, lon)

def remove_namespace(tree, ns):
    nsl = len(ns)
    for elem in tree.getiterator():
        if elem.tag.startswith(ns):
            elem.tag = elem.tag[nsl:]

dom = ET.parse(sys.argv[1])

course = dom.getroot()

namespace = course.tag.partition('}')[0]
remove_namespace(course, namespace + '}') # The } got pulled off in the partition

if course.tag == "gpx":
    step_gpx(course)
elif course.tag == 'TrainingCenterDatabase':
    step_tcx(course)
else:
    print("Not TCX or GPX.")
    print(course.tag)
    sys.exit(0)

# id is the Global Message Number
# Spec is an array of (type, field definition number, value)
# record_id is the FIT definition ID (0-15)
def write_field(id, spec, write_data = True, record_id = 0):
    # From table 4-6 in the spec
    # name -> base type field, size (bytes), python struct name
    types = {"enum": (0x00, 1, "B"),
            "sint8": (0x01, 1, "b"),
            "uint8": (0x02, 1, "B"),
            "sint16": (0x83, 2, "h"),
            "uint16": (0x84, 2, "H"),
            "sint32": (0x85, 4, "l"),
            "uint32": (0x86, 4, "L"),
            "string": (0x07, -1,"s"),
            "float32": (0x88, 4,"f"),
            "float64": (0x89, 8,"d"),
            "uint8z": (0x0a, 1,"B"),
            "uint16z": (0x8b, 2,"S"),
            "uint32z": (0x8c, 4,"L"),
            "byte": (0x0d, -1,"s")}
    ret = b""
    header = (record_id & 0x0f) | 0x40 # 0100<record_id>
    # Header, reserved, little endian, 
    ret += struct.pack("=BBBHB", header, 0, 0, id, len(spec))
    data = b""
    if write_data:
        data = struct.pack("=B", record_id)
    for elem in spec:
        # Field def #, Size, Base Type
        size_flag, size, size_type = types[elem[1]]
        if size == -1:
            size = len(elem[2])
            size_type = str(size) + "s"
        ret += struct.pack("=BBB", elem[0], size, size_flag)
        if write_data:
            data += struct.pack("=" + size_type, elem[2])
    return ret + data

out = open(sys.argv[2], "w+b")

# Write the standard FIT header
# Adjust out the size later
out.write(struct.pack("=BBHL4sH", 14, 0x10, 411, 0, b'.FIT', 0))

# Write out the standard definitions
# 0, file_id
out.write(write_field(0, [
    (0, "enum", 6), # type, 6 = course
    (1, "uint16", 1), # manufacturer
    (2, "uint16", 1), # product
    (3, "uint32z", 1), # serial
    (4, "uint32", int((datetime.datetime.utcnow() - epoch).total_seconds())) # time_created
        ]))

# 31, course
# Define 1 field (name, string)
out.write(write_field(31, [
    (5, "string", track_name.encode(encoding='ascii')) # Name
    ]))

# 19, Laps
# Define 2 fields (timestamp, start_time)
# This doesn't match up with the exported file...
out.write(write_field(19, [
    (253, "uint32", 0), #timestamp
    (2, "uint32", 0) #start_time
    ], False)) # Header only
# Spit out lap data
for lap in laps:
    out.write(struct.pack("=BLL", 0, lap[0], lap[1]))

# Throw in a start event for good measure
out.write(write_field(21, [
    (253, "uint32", trackpoints[0][0]),
    (0, "enum", 0),
    (4, "uint8", 0),
    (1, "enum", 0)]))


# Record (i.e. track point) = 20
# Define timestamp, distance, lat, long
out.write(write_field(20, [
    (253, "uint32", 0), # timestamp
    (0, "sint32", 0), # Latitude
    (1, "sint32", 0), # Longitude
    (5, "uint32", 0) # Distance
    ], False)) # Write definition only
# Spit out all the course points

# "=llL"
for point in trackpoints:
    out.write(struct.pack("=BLllL", 0, point[0], point[1], point[2], point[3]))

# End event
out.write(write_field(21, [
    (253, "uint32", trackpoints[-1][0]),
    (0, "enum", 0),
    (4, "uint8", 0),
    (1, "enum", 9)]))

# Calculate the checksum

out.seek(14, 0) # Skip over the header, not included in the calculation
def checksum(f):
    bytes = f.read()
    crc_table = [0x0, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
            0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    crc = 0
    count = 0
    for byte in bytes:
        count += 1
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]

        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]

    return crc

crc = checksum(out)

# Seek back to the start, rewrite the size bit
out.seek(0, 2) # FROM_END... defined somewhere
size = out.tell()
print("Size: %d bytes" % size)
out.write(struct.pack("=H", crc))
out.seek(4, 0) # To where the data size bit is stored
out.write(struct.pack("=L", size - 14)) # Size was measured before the checksum

out.close()

