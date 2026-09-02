# -*-coding:utf-8-*-

# this code is currently for python 2.7
from __future__ import print_function
from time import monotonic, sleep

try:
    import smbus
except ImportError:  # smbus2 is a drop-in replacement (pip) on newer Pi OS
    import smbus2 as smbus

# register addresses
REG_INTR_STATUS_1 = 0x00
REG_INTR_STATUS_2 = 0x01

REG_INTR_ENABLE_1 = 0x02
REG_INTR_ENABLE_2 = 0x03

REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08

REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C

REG_LED2_PA = 0x0D
REG_PILOT_PA = 0x10
REG_MULTI_LED_CTRL1 = 0x11
REG_MULTI_LED_CTRL2 = 0x12

REG_TEMP_INTR = 0x1F
REG_TEMP_FRAC = 0x20
REG_TEMP_CONFIG = 0x21
REG_PROX_INT_THRESH = 0x30
REG_REV_ID = 0xFE
REG_PART_ID = 0xFF


class MAX30102():
    # REG_PART_ID reads 0x15 on a genuine MAX30102. A MAX30100 (different
    # register map entirely) or a bus that reads back nothing answers
    # something else, and the symptom of that is a FIFO that never fills --
    # so the check is worth its one transaction at startup.
    PART_ID = 0x15

    # Seconds read_sequential() will wait with NOTHING arriving before it
    # gives up and returns a short buffer. Not a budget for the whole read:
    # 100 samples take 4 s at the configured rate, and the timer restarts
    # every time samples come in. At 25 Hz a sample is due every 40 ms, so
    # two silent seconds means the sensor has stopped, not that it is slow.
    STALL_TIMEOUT_SECONDS = 2.0

    # by default, this assumes that the device is at 0x57 on channel 1
    def __init__(self, channel=1, address=0x57):
        #print("Channel: {0}, address: {1}".format(channel, address))
        self.address = address
        self.channel = channel
        self.bus = smbus.SMBus(self.channel)
        # True when the last read came back short because the sensor went
        # quiet; callers use it to say "no data from the sensor" instead of
        # blaming the finger.
        self.last_read_timed_out = False

        self.reset()

        sleep(1)  # wait 1 sec

        # read & clear interrupt register (read 1 byte)
        reg_data = self.bus.read_i2c_block_data(self.address, REG_INTR_STATUS_1, 1)
        # print("[SETUP] reset complete with interrupt register0: {0}".format(reg_data))
        self.setup()
        # print("[SETUP] setup complete")

    def shutdown(self):
        """
        Shutdown the device.
        """
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [0x80])

    def reset(self):
        """
        Reset the device, this will clear all settings,
        so after running this, run setup() again.
        """
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [0x40])

    # LED drive current, in units of 0.2 mA (0x3F = 12.6 mA). The Maxim sample
    # code uses 0x24 (~7 mA), which on these breakout boards leaves the IR DC
    # level -- and with it the pulsatile AC amplitude the algorithm has to find
    # peaks in -- low enough that a real finger can read as "no finger". Raising
    # it costs nothing but LED power and lifts both.
    DEFAULT_LED_CURRENT = 0x3F

    def setup(self, led_mode=0x03, led_current=None):
        """
        This will setup the device with the values written in sample Arduino code.
        """
        if led_current is None:
            led_current = self.DEFAULT_LED_CURRENT

        # INTR setting
        # Interrupts drive the INT pin only, and nothing here is wired to it --
        # this driver polls the FIFO pointers instead. Leaving them disabled is
        # what makes it safe for read_fifo() to skip clearing the status
        # registers, which is 2 of every 3 I2C transactions it used to spend.
        self.bus.write_i2c_block_data(self.address, REG_INTR_ENABLE_1, [0x00])
        self.bus.write_i2c_block_data(self.address, REG_INTR_ENABLE_2, [0x00])

        # FIFO_WR_PTR[4:0]
        self.bus.write_i2c_block_data(self.address, REG_FIFO_WR_PTR, [0x00])
        # OVF_COUNTER[4:0]
        self.bus.write_i2c_block_data(self.address, REG_OVF_COUNTER, [0x00])
        # FIFO_RD_PTR[4:0]
        self.bus.write_i2c_block_data(self.address, REG_FIFO_RD_PTR, [0x00])

        # 0b 0100 1111
        # sample avg = 4, fifo rollover = false, fifo almost full = 17
        # Rollover stays OFF on purpose: with it on the FIFO would quietly
        # overwrite itself and the caller could never tell that the window it
        # is about to measure has a hole in it. Off, the sensor stops writing
        # and counts what it dropped -- see get_overflow_count().
        #
        # Either way a FULL FIFO parks the write pointer 32 samples ahead of
        # the read pointer, which in 5-bit pointer arithmetic is the SAME
        # value as empty: get_data_present() cannot tell "nothing yet" from
        # "32 samples waiting". The overflow counter is the only thing that
        # separates them, which is why read_sequential() consults it before
        # deciding to wait.
        self.bus.write_i2c_block_data(self.address, REG_FIFO_CONFIG, [0x4f])

        # 0x02 for read-only, 0x03 for SpO2 mode, 0x07 multimode LED
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [led_mode])
        # 0b 0010 0111
        # SPO2_ADC range = 4096nA, SPO2 sample rate = 100Hz, LED pulse-width = 411uS
        self.bus.write_i2c_block_data(self.address, REG_SPO2_CONFIG, [0x27])

        # LED1 (red) and LED2 (IR) drive current
        self.bus.write_i2c_block_data(self.address, REG_LED1_PA, [led_current])
        self.bus.write_i2c_block_data(self.address, REG_LED2_PA, [led_current])
        # choose value fro ~25mA for Pilot LED
        self.bus.write_i2c_block_data(self.address, REG_PILOT_PA, [0x7f])

        # A warning, not an exception: the part answers at 0x57 and the rest
        # of the setup already went through, so refusing to run would take a
        # working-but-unrecognised clone off the kiosk. Printing it means a
        # FIFO that never fills has an explanation at the top of the log.
        part = self.read_part_id()
        if part != self.PART_ID:
            print("[SpO2] warning: part id 0x{:02X} at address 0x{:02X}, "
                  "expected 0x{:02X} (MAX30102) -- register map may differ"
                  .format(part, self.address, self.PART_ID))

    def read_part_id(self):
        try:
            return self.bus.read_byte_data(self.address, REG_PART_ID)
        except Exception:
            return -1

    # this won't validate the arguments!
    # use when changing the values from default
    def set_config(self, reg, value):
        self.bus.write_i2c_block_data(self.address, reg, value)

    def get_data_present(self):
        read_ptr = self.bus.read_byte_data(self.address, REG_FIFO_RD_PTR)
        write_ptr = self.bus.read_byte_data(self.address, REG_FIFO_WR_PTR)
        if read_ptr == write_ptr:
            return 0
        else:
            num_samples = write_ptr - read_ptr
            # account for pointer wrap around
            if num_samples < 0:
                num_samples += 32
            return num_samples

    def get_overflow_count(self):
        """Samples the FIFO dropped because it filled up before being read.

        FIFO rollover is off (see FIFO_CONFIG in setup), so a full FIFO stops
        accepting samples until it is drained: the buffer stays internally
        contiguous but a gap opens in TIME, which is invisible in the data and
        quietly corrupts both the peak-to-peak interval (heart rate) and the
        AC/DC ratio (SpO2). Callers use this to throw such a window away rather
        than compute a plausible-looking wrong answer from it."""
        return self.bus.read_byte_data(self.address, REG_OVF_COUNTER) & 0x1F

    def clear_overflow_count(self):
        self.bus.write_i2c_block_data(self.address, REG_OVF_COUNTER, [0x00])

    def reset_fifo(self):
        """Throw away everything the FIFO holds and clear the overflow count.

        The chip samples continuously from setup() onwards, and with rollover
        off the 32-deep FIFO fills in 1.28 s at the 25 Hz output rate. So by
        the time a caller actually starts measuring, the buffer is already
        full of samples taken before the finger arrived AND the overflow
        counter is already set -- which is why the first window of every
        measurement used to be built from stale data and then discarded by
        the overflow check anyway.

        Zeroing all three pointers together is the datasheet's way of
        starting a fresh acquisition; the same three writes setup() does."""
        self.bus.write_i2c_block_data(self.address, REG_FIFO_WR_PTR, [0x00])
        self.bus.write_i2c_block_data(self.address, REG_OVF_COUNTER, [0x00])
        self.bus.write_i2c_block_data(self.address, REG_FIFO_RD_PTR, [0x00])

    def read_fifo(self):
        """
        This function will read the data register.
        """
        red_led = None
        ir_led = None

        # The interrupt status registers used to be read (and discarded) on
        # every single sample. Interrupts are disabled in setup() and the INT
        # pin is unused, so nothing needs clearing -- dropping those two reads
        # cuts this function from 3 I2C transactions per sample to 1, which is
        # what keeps the FIFO drained fast enough to avoid the overflow gap
        # that get_overflow_count() exists to catch.

        # read 6-byte data from the device
        d = self.bus.read_i2c_block_data(self.address, REG_FIFO_DATA, 6)

        # mask MSB [23:18]
        red_led = (d[0] << 16 | d[1] << 8 | d[2]) & 0x03FFFF
        ir_led = (d[3] << 16 | d[4] << 8 | d[5]) & 0x03FFFF

        return red_led, ir_led

    def restart_fifo(self):
        """Drop what the FIFO holds and start filling from the top again.

        Deliberately leaves the overflow counter alone: that is what tells the
        caller the next window spans a gap and has to be thrown away."""
        self.bus.write_i2c_block_data(self.address, REG_FIFO_WR_PTR, [0x00])
        self.bus.write_i2c_block_data(self.address, REG_FIFO_RD_PTR, [0x00])

    def read_sequential(self, amount=110):
        """Read `amount` samples, or fewer if the sensor stops producing them.

        This used to be `while count > 0` around a 10 ms sleep with no way
        out, so a sensor that never filled its FIFO hung the caller forever --
        and with it the settling loop's own deadline, which is only checked
        BETWEEN reads. Running spo2_monitor.py then printed its opening line
        and nothing else, for ever.

        Two very different things produce a FIFO that reads as empty, so they
        are separated here rather than waited on identically:

        * the sensor really has nothing yet -- wait, up to
          STALL_TIMEOUT_SECONDS of continuous silence, then return short and
          leave `last_read_timed_out` set;
        * the FIFO is FULL. Rollover is off (see setup), so a full FIFO stops
          accepting samples with its write pointer parked exactly 32 ahead of
          the read pointer -- the same value, in 5-bit pointer arithmetic, as
          empty. Waiting for that to clear waits for ever, because nothing
          drains it but us; the overflow counter is what distinguishes it, and
          the fix is to restart the FIFO and let the caller discard the window
          the gap falls in."""
        red_buf = []
        ir_buf = []
        count = amount
        self.last_read_timed_out = False
        deadline = monotonic() + self.STALL_TIMEOUT_SECONDS
        while count > 0:
            num_bytes = self.get_data_present()
            if num_bytes == 0:
                if self.get_overflow_count():
                    self.restart_fifo()
                    continue
                if monotonic() >= deadline:
                    self.last_read_timed_out = True
                    break
                sleep(0.01)
                continue
            while num_bytes > 0:
                red, ir = self.read_fifo()
                red_buf.append(red)
                ir_buf.append(ir)
                num_bytes -= 1
                count -= 1
            # samples are flowing, so the silence timer starts over -- the
            # timeout is for a sensor that stopped, not for a slow one
            deadline = monotonic() + self.STALL_TIMEOUT_SECONDS
        return red_buf, ir_buf
