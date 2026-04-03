meta:
  id: HUCSat
  title: HUCSat beacon telemetry
  file-extension: HUCSat
  endian: be

doc: |
  Packet: [6-byte PacketManager header][TLV field]...[TLV field]
  Each TLV field: [4B key_hash=hash(key)&0xFFFFFFFF][1B type_id][variable value]
  Field order is fixed (OrderedDict). Numeric fields use float32; vectors
  (av, acc, magn_v) are split into _0/_1/_2 components when valid, or a
  single string "None" field when the sensor is unavailable.

  :field pkt_id:         header.pkt_id
  :field seq_num:        header.seq_num
  :field total_pkts:     header.total_pkts
  :field rssi:           header.rssi
  :field name:           name.value.value
  :field FSM_state:      fsm_state.value.value
  :field FSM_depl:       fsm_depl.value.value
  :field FSM_pay_set:    fsm_pay_set.value.value
  :field FSM_pan_light:  fsm_pan_light.value.value
  :field FSM_payl_light: fsm_payl_light.value.value
  :field FSM_best_dir:   fsm_best_dir.value.value
  :field FSM_batt_v:     fsm_batt_v.value.value
  :field FSM_av_0:       fsm_av_0.value.value
  :field FSM_av_1:       fsm_av_1.value.value
  :field FSM_av_2:       fsm_av_2.value.value
  :field FSM_acc_0:      fsm_acc_0.value.value
  :field FSM_acc_1:      fsm_acc_1.value.value
  :field FSM_acc_2:      fsm_acc_2.value.value
  :field FSM_magn_v_0:   fsm_magn_v_0.value.value
  :field FSM_magn_v_1:   fsm_magn_v_1.value.value
  :field FSM_magn_v_2:   fsm_magn_v_2.value.value
  :field time:           time.value.value
  :field uptime:         uptime.value.value

seq:
  - id: header
    type: packet_header
  - id: name
    type: tlv_field
    doc: "Satellite name"
  - id: fsm_state
    type: tlv_field
    doc: "Flight state machine current state (bootup/detumble/deploy/orient)"
  - id: fsm_depl
    type: tlv_field
    doc: "Deployment status (0.0 = not deployed, 1.0 = deployed)"
  - id: fsm_pay_set
    type: tlv_field
    doc: "Payload enable setting"
  - id: fsm_pan_light
    type: tlv_field
    doc: "Panel face light intensities (stringified list of 5 values)"
  - id: fsm_payl_light
    type: tlv_field
    doc: "Payload light intensity"
  - id: fsm_best_dir
    type: tlv_field
    doc: "Best orientation direction index (-1.0 if no direction is better)"
  - id: fsm_batt_v
    type: tlv_field
    doc: "Battery voltage (V)"
  - id: fsm_av_0
    type: tlv_field
    doc: "Angular velocity X (rad/s)"
  - id: fsm_av_1
    type: tlv_field
    doc: "Angular velocity Y (rad/s)"
  - id: fsm_av_2
    type: tlv_field
    doc: "Angular velocity Z (rad/s)"
  - id: fsm_acc_0
    type: tlv_field
    doc: "Acceleration X (m/s^2)"
  - id: fsm_acc_1
    type: tlv_field
    doc: "Acceleration Y (m/s^2)"
  - id: fsm_acc_2
    type: tlv_field
    doc: "Acceleration Z (m/s^2)"
  - id: fsm_magn_v_0
    type: tlv_field
    doc: "Magnetometer X (uT)"
  - id: fsm_magn_v_1
    type: tlv_field
    doc: "Magnetometer Y (uT)"
  - id: fsm_magn_v_2
    type: tlv_field
    doc: "Magnetometer Z (uT)"
  - id: time
    type: tlv_field
    doc: "OBC timestamp (YYYY-MM-DD HH:MM:SS)"
  - id: uptime
    type: tlv_field
    doc: "Seconds since boot"

types:
  packet_header:
    seq:
      - id: pkt_id
        type: u1
        doc: "Packet type identifier"
      - id: seq_num
        type: u2
        doc: "Packet sequence number"
      - id: total_pkts
        type: u2
        doc: "Total number of packets in the transmission"
      - id: rssi
        type: u1
        doc: "Last received RSSI (absolute value)"

  tlv_field:
    seq:
      - id: key_hash
        type: u4
        doc: "CircuitPython hash(key) & 0xFFFFFFFF"
      - id: type_id
        type: u1
        enum: field_type
        doc: "Field value data type"
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

  tlv_string:
    seq:
      - id: length
        type: u1
        doc: "String length in bytes"
      - id: value
        type: str
        size: length
        encoding: UTF-8
        doc: "UTF-8 string content"

  tlv_int8:
    seq:
      - id: value
        type: s1

  tlv_int16:
    seq:
      - id: value
        type: s2

  tlv_int32:
    seq:
      - id: value
        type: s4

  tlv_int64:
    seq:
      - id: value
        type: s8

  tlv_float32:
    seq:
      - id: value
        type: f4

  tlv_float64:
    seq:
      - id: value
        type: f8

  tlv_uint8:
    seq:
      - id: value
        type: u1

  tlv_uint16:
    seq:
      - id: value
        type: u2

  tlv_uint32:
    seq:
      - id: value
        type: u4

  tlv_uint64:
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
