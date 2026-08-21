try:
    from .max30102 import MAX30102
    from .hrcalc import calc_hr_and_spo2
except ImportError:
    from max30102 import MAX30102
    from hrcalc import calc_hr_and_spo2

import time
import numpy as np

class SpO2Monitor():
    def __init__(self):
        self.bpm = 0
        self.m = MAX30102()

    def GetSpO2Sensor(self):
        hr2 = 0
        sp2 = 0

        red, ir = self.m.read_sequential()
        hr, hrb, sp, spb = calc_hr_and_spo2(np.array(ir), np.array(red))

        if hrb == True and hr != -999:
            hr2 = int(hr)
        if spb == True and sp != -999:
            sp2 = int(sp)

        return hr2, sp2, red, ir

def main():
    print('sensor starting...')
    SpO2_Sensor = SpO2Monitor()
    LOOP_TIME = 1

    while True:
        bpm, spo2, red, ir = SpO2_Sensor.GetSpO2Sensor()
        print("BPM: {}, SpO2: {}".format(bpm, spo2))
        time.sleep(LOOP_TIME)

if __name__ == '__main__':
    main()