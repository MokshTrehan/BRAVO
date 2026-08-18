#!/usr/bin/env python3
import rosbag, sys, os
for path in sys.argv[1:]:
    b = rosbag.Bag(path); last={}; worst={}; count={}
    for topic, msg, t in b.read_messages():
        ts = t.to_sec(); count[topic] = count.get(topic,0)+1
        if topic in last:
            g = ts - last[topic]
            if g > worst.get(topic,(0,0,0))[0]: worst[topic]=(g,last[topic],ts)
        last[topic]=ts
    b.close()
    print("== %s ==" % path.replace('/home/moksh/datasets/KAIST_VIO/raw/',''))
    for topic in sorted(count):
        g,t0,t1 = worst.get(topic,(0,0,0))
        print("  %-40s n=%6d  max_gap=%9.3fs  [%.6f -> %.6f]" % (topic,count[topic],g,t0,t1))
