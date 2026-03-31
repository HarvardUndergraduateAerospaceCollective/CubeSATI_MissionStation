meta:
  id: cubesati
  file-extension: cubesati
  endian: be
  license: MIT

doc: |
  Kaitai Struct decoder for CubeSAT-I (Harvard Undergraduate Aerospace Collective)
  beacon telemetry packets. Compatible with the TinyGS ground station network.

  The satellite uses a PROVES Kit RP2350 v5d flight controller running
  CircuitPython. Beacons are encoded using a BinaryEncoder (key-hash TLV
  format) and wrapped in a 6-byte PacketManager radio frame header.

  Packet layout:
    [6-byte header][TLV field 0][TLV field 1]...[TLV field N]

  Each TLV field:
    [4-byte key_hash (uint32)][1-byte type_id][variable-length value]

  Key hashes are computed on the satellite as: hash(key) & 0xFFFFFFFF
  using CircuitPython's hash() function.

  Expected beacon fields (in transmission order):
    name          (string)  - Satellite name, e.g. "CubeSATI"
    FSM_state     (string)  - Flight State Machine state
    FSM_depl      (int)     - Deployment status
    FSM_pay_set   (int)     - Payload setting
    FSM_pan_light (int/flt) - Panel light intensity
    FSM_payl_light(int/flt) - Payload light intensity
    FSM_best_dir  (int)     - Best orientation direction
    FSM_magn_v_0  (float)   - Magnetometer X (uT)
    FSM_magn_v_1  (float)   - Magnetometer Y (uT)
    FSM_magn_v_2  (float)   - Magnetometer Z (uT)
    FSM_av_0      (float)   - Angular velocity X (deg/s)
    FSM_av_1      (float)   - Angular velocity Y (deg/s)
    FSM_av_2      (float)   - Angular velocity Z (deg/s)
    FSM_acc_0     (float)   - Acceleration X (m/s2)
    FSM_acc_1     (float)   - Acceleration Y (m/s2)
    FSM_acc_2     (float)   - Acceleration Z (m/s2)
    FSM_batt_v    (float)   - Battery voltage (V)
    time          (string)  - OBC formatted timestamp
    uptime        (float)   - Seconds since boot

  Reference: https://github.com/HarvardUndergraduateAerospaceCollective/OBC_v5d

seq:
  - id: header
    type: packet_header
    doc: 6-byte PacketManager radio frame header
  - id: fields
    type: tlv_field
    repeat: eos
    doc: Repeating TLV-encoded telemetry fields until end of packet

types:
  packet_header:
    doc: |
      6-byte header prepended by the PacketManager to every radio frame.
      Used for multi-fragment reassembly and signal quality tracking.
    seq:
      - id: packet_identifier
        type: u1
        doc: Message counter / packet sequence ID
      - id: sequence_number
        type: u2
        doc: Fragment sequence number (0-based) within a multi-part beacon
      - id: total_packets
        type: u2
        doc: Total number of fragments in this beacon transmission
      - id: rssi_abs
        type: u1
        doc: Absolute value of RSSI at transmission time (dBm)

  tlv_field:
    doc: |
      A single key-value telemetry field encoded in Type-Length-Value format.
      The key is a 4-byte hash of the field name. The type_id determines the
      encoding of the value that follows.
    seq:
      - id: key_hash
        type: u4
        doc: |
          Hash of the field name: hash(key) & 0xFFFFFFFF
          Computed using CircuitPython's hash() on the satellite.
      - id: type_id
        type: u1
        doc: Data type identifier (see type_id enum for values)
        enum: field_type
      - id: value
        type:
          switch-on: type_id
          cases:
            'field_type::string':    tlv_string
            'field_type::int8':      tlv_int8
            'field_type::int16':     tlv_int16
            'field_type::int32':     tlv_int32
            'field_type::int64':     tlv_int64
            'field_type::float32':   tlv_float32
            'field_type::float64':   tlv_float64
            'field_type::uint8':     tlv_uint8
            'field_type::uint16':    tlv_uint16
            'field_type::uint32':    tlv_uint32
            'field_type::uint64':    tlv_uint64
        doc: Value payload, format determined by type_id

  tlv_string:
    doc: Length-prefixed UTF-8 string value
    seq:
      - id: length
        type: u1
        doc: Length of the UTF-8 string in bytes
      - id: value
        type: str
        size: length
        encoding: UTF-8

  tlv_int8:
    doc: Signed 8-bit integer
    seq:
      - id: value
        type: s1

  tlv_int16:
    doc: Signed 16-bit integer (big-endian)
    seq:
      - id: value
        type: s2

  tlv_int32:
    doc: Signed 32-bit integer (big-endian)
    seq:
      - id: value
        type: s4

  tlv_int64:
    doc: Signed 64-bit integer (big-endian)
    seq:
      - id: value
        type: s8

  tlv_float32:
    doc: IEEE 754 single-precision float (big-endian)
    seq:
      - id: value
        type: f4

  tlv_float64:
    doc: IEEE 754 double-precision float (big-endian)
    seq:
      - id: value
        type: f8

  tlv_uint8:
    doc: Unsigned 8-bit integer
    seq:
      - id: value
        type: u1

  tlv_uint16:
    doc: Unsigned 16-bit integer (big-endian)
    seq:
      - id: value
        type: u2

  tlv_uint32:
    doc: Unsigned 32-bit integer (big-endian)
    seq:
      - id: value
        type: u4

  tlv_uint64:
    doc: Unsigned 64-bit integer (big-endian)
    seq:
      - id: value
        type: u8

enums:
  field_type:
    0: string
    1: int8
    2: int16
    3: int32
    4: int64
    5: float32
    6: float64
    11: uint8
    12: uint16
    13: uint32
    14: uint64
