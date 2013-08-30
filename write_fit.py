#!/usr/bin/python
# vim: et sw=4 ts=4

import sys
import struct
import datetime
import time
import binascii
import math
from xml.dom.minidom import parse

epoch = datetime.datetime(1989, 12, 31, 0, 0, 0)

fin = open(sys.argv[1])
dom = parse(fin)

course = dom.firstChild

track_name = ""
laps = []
trackpoints = []

def degree_to_semicircle(degree):
    return int(degree * (2**31 / 180))

def extract_child(node, name):
    return node.getElementsByTagName(name)[0].firstChild.data

def step_tcx(data):
    partial_laps = []
    # Lap start _time_ needs to be logged... same as the timestamp?
    def track_point(node):
        time_raw = extract_child(node, "Time")
        # Garmin Connect  includes .%f, RideWithGPS doesn't.
        if '.' in time_raw:
            format = "%Y-%m-%dT%H:%M:%S.%fZ"
        else:
            format = "%Y-%m-%dT%H:%M:%S"
        time = datetime.datetime.strptime(time_raw, format)
        etime = time - epoch
        distance = int(float(extract_child(node, "DistanceMeters"))*100)
        points = node.getElementsByTagName("Position")[0]
        lat = float(extract_child(points, "LatitudeDegrees"))
        lon = float(extract_child(points, "LongitudeDegrees"))
        return (int(etime.total_seconds()), degree_to_semicircle(lat),
                degree_to_semicircle(lon), distance)

    for element in data.childNodes:
        if element.nodeName != "Courses":
            continue
        # Going to assume there can only be one course in a file...
        course = element.getElementsByTagName("Course")[0]
        for part in course.childNodes:
            if part.nodeName == "Name":
                global track_name
                track_name = part.firstChild.data + "\0"
            elif part.nodeName == "Lap":
                time = 0.0
                distance = 0
                start = (0.0, 0.0)
                for attr in part.childNodes:
                    if attr.nodeName == "TotalTimeSeconds":
                        time = float(attr.firstChild.data)
                    elif attr.nodeName == "DistanceMeters":
                        distance = int(float(attr.firstChild.data)*100)
                    elif attr.nodeName == "BeginPosition":
                        lat = float(extract_child(attr, 'LatitudeDegrees'))
                        lon = float(extract_child(attr, 'LongitudeDegrees'))
                        start = (degree_to_semicircle(lat), degree_to_semicircle(lon))
                partial_laps.append((time, distance, start))
                # Find the first location that matches?
            elif part.nodeName == "Track":
                points = part.getElementsByTagName("Trackpoint")
                for tp in points:
                    trackpoints.append(track_point(tp))

    # Crunch the lap data - map each point to the first track point that matches in order to steal the timestamp
    for lap in partial_laps:
        for point in trackpoints:
            if lap[2][0] == point[1] and lap[2][1] == point[2]:
                laps.append((0, point[0]))
                break

# GPX samples don't have distance, but FIT requires it. Simple estimation of distance
# Could be replaced with Haversine, but probably won't make much difference.
# p1, p2 are (lat, lon) in degrees
# Result is in cm
def distance_ll(p1, p2):
    p1 = [x*math.pi/180.0 for x in p1]
    p2 = [x*math.pi/180.0 for x in p2]
    x = (p2[1]-p1[1]) * math.cos((p1[0]+p1[1])/2);
    y = (p2[0] - p1[0]);
    return int(math.hypot(x, y) * 6.3675e8); # Radius of the earth in km

def step_gpx(data):
# Hrmmmm, GPX data doesn't have distances included...

    for element in data.childNodes:
        if element.nodeName != "trk":
            continue
        last_point = None
        time = 0 # Arbitrary... just fill it in randomly
        for node in element.childNodes:
            if node.nodeName == "name":
                global track_name
                track_name = node.firstChild.data + "\0"
            elif node.nodeName == "trkseg":
                for point in node.childNodes:
                    if point.nodeName != "trkpt":
                        continue
                    lat = float(point.attributes['lat'].value)
                    lon = float(point.attributes['lon'].value)
                    if last_point:
                        distance = distance_ll((lat, lon), last_point)
                    else:
                        distance = 0

                    # Write out point
                    trackpoints.append((time, degree_to_semicircle(lat), degree_to_semicircle(lon), distance))
                    time += 100
                    last_point = (lat, lon)

if course.nodeName == "gpx":
    step_gpx(course)
    print(track_name)
elif course.nodeName == "TrainingCenterDatabase": # == "tcx"
    step_tcx(course)

# print(laps)
# print(trackpoints)

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
    (4, "uint32", int(time.time())) # time_created
        ]))

# 31, course
# Define message 0, Reserved, little_endian, file_id = 31, 1 field
# Define 1 field (name, string)
out.write(write_field(31, [
    (5, "string", track_name.encode(encoding='ascii')) # Name
    ]))

# 19, Laps
# Define message 0, _, little endian, file_id = 19, 2 fields
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


# Record (i.e. track point)
# Define message 0, _, little endian, file_id = 20, 4 fields
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

