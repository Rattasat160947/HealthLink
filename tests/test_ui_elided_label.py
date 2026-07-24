# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from carekeeper_ui import ElidedLabel


def test_elided_label_preserves_full_text_and_prefers_full_width(qtbot):
    """Regression: the header name once got stuck truncated because eliding fed
    a shrunken width back into sizeHint. sizeHint must track the FULL text (so a
    shown page gets enough room), while minimumSizeHint stays 0 (so a genuinely
    narrow row can still shrink it), and text() always returns the real value."""
    name = "นายสมชาย ใจดี (Mr. Somchai Jaidee)"
    label = ElidedLabel(name)
    qtbot.addWidget(label)

    assert label.text() == name
    assert label.minimumSizeHint().width() == 0
    assert label.sizeHint().width() > 50


def test_elided_label_truncates_display_when_too_narrow(qtbot):
    """When the width is smaller than the text, the displayed (base QLabel) text
    is elided, but the logical text() is still the full string."""
    name = "นางสาวประภัสสรวรรณ ศรีสุพรรณเมธากุลวงศ์ (Ms. Praphatsornwan)"
    label = ElidedLabel(name)
    qtbot.addWidget(label)
    label.resize(80, 24)
    label._elide_to_width()  # what resizeEvent runs; called directly for determinism

    # base QLabel text (what is painted) is shortened; ElidedLabel.text() is full
    assert QLabel.text(label) != name
    assert label.text() == name
