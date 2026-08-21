from dataclasses import dataclass
from smartcard.System import readers
from smartcard.CardConnection import CardConnection

from .apdu import *


@dataclass
class CardInfo:
    cid: str
    th_name: str
    en_name: str
    birth_date: str
    address: str


class ThaiIDCard:

    def __init__(self):
        self.conn = None

    def connect(self):

        r = readers()

        if not r:
            raise RuntimeError("No Smart Card Reader Found")

        self.conn = r[0].createConnection()

        try:
            self.conn.connect(CardConnection.T1_protocol)
        except:
            self.conn.connect()

        self._select_card()

    def _transmit(self, apdu):

        data, sw1, sw2 = self.conn.transmit(apdu)

        #
        # GET RESPONSE
        #
        if sw1 == 0x61:

            get_response = [
                0x00,
                0xC0,
                0x00,
                0x00,
                sw2
            ]

            data, sw1, sw2 = self.conn.transmit(
                get_response
            )

        return data, sw1, sw2

    def _select_card(self):

        data, sw1, sw2 = self._transmit(SELECT)

        if (sw1, sw2) != (0x90, 0x00):
            raise RuntimeError(
                f"Select Card Failed {sw1:02X} {sw2:02X}"
            )

    def _read_apdu(self, cmd):

        data, sw1, sw2 = self._transmit(cmd)

        if (sw1, sw2) != (0x90, 0x00):
            raise RuntimeError(
                f"APDU Error {sw1:02X} {sw2:02X}"
            )

        return bytes(data)

    def _decode(self, raw):

        return (
            raw.decode("tis-620", errors="ignore")
            .replace("#", " ")
            .strip()
        )

    def get_cid(self):

        return self._decode(
            self._read_apdu(CID)
        )

    def get_th_name(self):

        return self._decode(
            self._read_apdu(TH_NAME)
        )

    def get_en_name(self):

        return self._decode(
            self._read_apdu(EN_NAME)
        )

    def get_birth_date(self):

        return self._decode(
            self._read_apdu(BIRTH)
        )

    def get_address(self):

        return self._decode(
            self._read_apdu(ADDRESS)
        )

    def read(self):

        self.connect()

        return CardInfo(
            cid=self.get_cid(),
            th_name=self.get_th_name(),
            en_name=self.get_en_name(),
            birth_date=self.get_birth_date(),
            address=self.get_address()
        )