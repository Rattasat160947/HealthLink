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
    # by default, this assumes that the device is at 0x57 on channel 1
    def __init__(self, channel=1, address=0x57):
        #print("Channel: {0}, address: {1}".format(channel, address))
        self.address = address
        self.channel = channel
        self.bus = smbus.SMBus(self.channel)

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
        # Rollover stays OFF on purpose: with it on, a wrapped FIFO leaves the
        # read and write pointers equal, which is indistinguishable from an
        # empty one, so get_data_present() would report "no data" while holding
        # 32 stale samples. Off, an overflow is detectable instead --
        # see get_overflow_count().
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

    def flush_fifo(self):
        """Drop whatever the FIFO holds, WITHOUT touching the write side.

        The chip samples continuously from setup() onwards, and with rollover
        off the 32-deep FIFO fills in 1.28 s at the 25 Hz output rate. So by
        the time a caller actually starts measuring, the buffer is already
        full of samples taken before the finger arrived and the overflow
        counter is already set, and the first window of the measurement is
        built from that stale data and then discarded by the overflow check.

        The buffer is emptied by READING it out and throwing the samples
        away -- the same read_fifo() calls an ordinary measurement makes, which
        advance the read pointer through the part's own logic. At most 31 of
        them, so about 30 ms.

        Writing the pointers directly would be quicker, and zeroing all three
        is the datasheet's INIT sequence, but that is not safe to issue against
        a part already mid-acquisition: it moves the pointers out from under
        the chip's own full/empty tracking, and a FIFO that then reads back
        WR == RD cannot be told apart from an empty one. That is the same
        ambiguity setup() disables rollover to avoid, and read_sequential()
        answers it by waiting for samples which never arrive -- a measurement
        that hangs with no value and no error. Draining through the normal
        read path cannot get the part into that state, because it is the path
        the part is in the middle of anyway."""
        pending = self.get_data_present()
        while pending > 0:
            self.read_fifo()
            pending -= 1
        self.clear_overflow_count()

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

    # No sample has arrived for this long => the FIFO is not advancing.
    # Generous next to the 40 ms sample interval, so ordinary I2C contention
    # (the UPS HAT shares this bus) never trips it.
    NO_DATA_TIMEOUT = 1.0

    def read_sequential(self, amount=110, no_data_timeout=None):
        """Read `amount` samples, or give up once the FIFO stops advancing.

        This used to loop until data arrived, with no way out, and nothing
        above it could recover: measure_spo2() tests its deadline at the top of
        its own loop, which a call that never returns never reaches. So a part
        that stopped producing samples -- for any reason -- presented as a
        measurement that hung forever with no value, no error and no timeout.

        Returning short instead hands control back so the caller's deadline can
        do its job. A partly filled window already has a meaning upstream: it
        is skipped as "not enough signal buffered yet"."""
        if no_data_timeout is None:
            no_data_timeout = self.NO_DATA_TIMEOUT
        red_buf = []
        ir_buf = []
        count = amount
        last_sample = monotonic()
        while count > 0:
            num_bytes = self.get_data_present()
            if num_bytes == 0:
                if monotonic() - last_sample >= no_data_timeout:
                    break
                sleep(0.01)
                continue
            while num_bytes > 0:
                red, ir = self.read_fifo()
                red_buf.append(red)
                ir_buf.append(ir)
                num_bytes -= 1
                count -= 1
            last_sample = monotonic()
        return red_buf, ir_buf
