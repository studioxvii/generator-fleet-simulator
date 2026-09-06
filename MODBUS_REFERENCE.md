# Generator Fleet Simulator — Modbus TCP Reference

## Connection Details

| Parameter       | Value              |
|-----------------|--------------------|
| Protocol        | Modbus TCP         |
| Host            | `127.0.0.1` by default in source mode |
| Base port       | **5020** |
| Wire unit IDs   | **1–255 per port**; zero is not used |
| Function Codes  | FC 3 (Read Holding Registers), FC 6 (Write Single Register), FC 16 (Write Multiple Registers) |

The container binds internally to `0.0.0.0`. Compose publishes to loopback by
default. Keep all control ports local or on an isolated LAN. Access grants control.

## Fleet addressing

Modbus TCP has a one-byte Unit Identifier. The simulator assigns 255 generators
per TCP port, then increments the port and restarts the wire unit ID at 1.
Global generator IDs in the dashboard and REST API do not change.

```text
port = base_port + (generator_id - 1) // 255
wire_unit_id = 1 + (generator_id - 1) % 255
```

| Generator ID | Source port | Compose host port | Wire unit ID |
|---|---|---|---|
| 1 | 5020 | 5021 | 1 |
| 255 | 5020 | 5021 | 255 |
| 256 | 5021 | 5022 | 1 |
| 510 | 5021 | 5022 | 255 |
| 511 | 5022 | 5023 | 1 |
| 2000 | 5027 | 5028 | 215 |

Only the ports required by the configured fleet listen inside the app. Compose
reserves the full eight-port host range. Custom mappings must publish consecutive
ports and set `GENSIM_PUBLIC_MODBUS_PORT` to the first host port. Configuration
rejects ranges above 65535. A busy additional port rejects fleet expansion.
The separate BESS/PV demo publishes Generator Fleet on ports 5030–5037 to avoid
its other simulators' ports.

`GET /api/modbus/endpoints` lists active ranges. The register API, Modbus tab,
and configuration CSV show the exact port and wire unit ID for each generator.
Some clients and gateways restrict IDs to 1–247 and cannot address the last
eight IDs on a full port. Use a client that accepts 1–255 for full-fleet access.

Source: [Modbus TCP implementation guide](https://www.modbus.org/file/secure/messagingimplementationguide.pdf).

## Register Map (Holding Registers, per Unit ID)

| Register | Description          | Data Type | Scaling   | Engineering Range       |
|----------|----------------------|-----------|-----------|-------------------------|
| 0        | Output kW            | UINT16    | ÷ 10      | 0.0 – rated kW         |
| 1        | Utility Load kW      | UINT16    | ÷ 10      | 0.0 – site load kW     |
| 2        | Utility Breaker      | UINT16    | raw       | 0 = Open, 1 = Closed   |
| 3        | Gen Breaker          | UINT16    | raw       | 0 = Open, 1 = Closed   |
| 4        | Fuel Level %         | UINT16    | ÷ 10      | 0.0 – 100.0 %          |
| 5        | Run Hours (whole)    | UINT16    | raw       | 0 – 65535 hours         |
| 6        | Run Hours (fraction) | UINT16    | ÷ 10000   | 0.0000 – 0.9999        |
| 7        | Coolant Temp         | UINT16    | raw       | 0 – 300 °F             |
| 8        | Oil Pressure         | UINT16    | raw       | 0 – 100 PSI            |
| 9        | Battery Voltage      | UINT16    | ÷ 10      | 0.0 – 40.0 V           |
| 10       | Engine RPM           | UINT16    | raw       | 0 – 2000 RPM           |
| 11       | Output Voltage       | UINT16    | raw       | 0 – 600 V              |
| 12       | Output Frequency     | UINT16    | ÷ 10      | 0.0 – 70.0 Hz          |
| 13       | Alarm Word 1         | UINT16    | bitfield  | see Alarm Bits below    |
| 14       | Alarm Word 2         | UINT16    | bitfield  | see Alarm Bits below    |
| 15       | Transfer Mode        | UINT16    | raw       | 0 = Transfer, 1 = Parallel, 2 = Island |
| 16       | Parallel Setpoint kW | UINT16    | ÷ 10      | 0.0 – rated kW         |
| 17       | Alarm Word 3         | UINT16    | bitfield  | see Alarm Bits below    |
| 18–19    | (Reserved)           | —         | —         | —                       |
| 20       | Command Register     | UINT16    | raw       | see Commands below      |

### Reconstructing Run Hours
```
Total Run Hours = Register 5 + (Register 6 / 10000)
Example: Reg 5 = 1234, Reg 6 = 5678 → 1234.5678 hours
```

### Scaling Examples
```
Output kW:       Register 0 value = 3500 → 3500 / 10 = 350.0 kW
Fuel Level:      Register 4 value = 875  → 875 / 10  = 87.5 %
Battery Voltage: Register 9 value = 285  → 285 / 10  = 28.5 V
Frequency:       Register 12 value = 600 → 600 / 10  = 60.0 Hz
```

---

## Alarm Bitfields

### Alarm Word 1 (Register 13)

| Bit | Alarm              | Type     | Trigger Condition        |
|-----|--------------------|----------|--------------------------|
| 0   | Engine Running     | Status   | Engine is running        |
| 1   | Ready              | Status   | Stopped, no faults       |
| 2   | Auto Mode          | Status   | Auto mode enabled        |
| 3   | E-Stop             | Critical | E-Stop command issued     |
| 4   | High Coolant Temp  | Critical | Coolant > 220°F          |
| 5   | Low Oil Pressure   | Critical | Oil pressure < 25 PSI    |
| 6   | Overspeed          | Critical | RPM > 1950               |
| 7   | Overcrank          | Fault    | Failed to start          |
| 8   | Low Coolant Level  | Warning  | (simulated fault)        |
| 9   | High Battery V     | Warning  | Battery > 31 V           |
| 10  | Low Battery V      | Warning  | Battery < 24 V           |
| 11  | Low Fuel           | Warning  | Fuel < 15%               |
| 12  | Over Voltage       | Warning  | Voltage > 510 V          |
| 13  | Under Voltage      | Warning  | Voltage < 450 V          |
| 14  | Over Frequency     | Warning  | Frequency > 63 Hz        |
| 15  | Under Frequency    | Warning  | Frequency < 57 Hz        |

### Alarm Word 2 (Register 14)

| Bit | Alarm              | Type     | Trigger Condition        |
|-----|--------------------|----------|--------------------------|
| 0   | Overload           | Warning  | Output > 105% of rated kW |
| 1   | Ground Fault       | Fault    | (simulated fault)        |

### Alarm Word 3 (Register 17)

| Bit | Alarm                 | Type     | Trigger Condition                  |
|-----|-----------------------|----------|------------------------------------|
| 0   | Reverse Power         | Critical | Output kW negative in parallel     |
| 1   | Sync Check Fail       | Fault    | Injectable only                    |
| 2   | Load Imbalance        | Warning  | Output deviates >20% from setpoint |
| 3   | Fuel Leak Detected    | Warning  | Fuel drops faster than expected    |
| 4   | Air Filter Restricted | Warning  | Injectable only                    |
| 5   | Exhaust High Temp     | Warning  | Injectable only                    |
| 6   | Gen Bearing Temp      | Critical | Injectable only                    |
| 7   | Overcurrent           | Critical | Injectable only                    |
| 8   | Loss of Field         | Critical | Injectable only                    |
| 9   | Utility Phase Loss    | Warning  | Injectable only                    |
| 10  | Parallel Sync Loss    | Fault    | Injectable only                    |
| 11  | Setpoint Not Reached  | Warning  | Output stays below 90% of setpoint |

### Decoding Example
```
Register 13 = 0x0005 = 0b0000000000000101
  Bit 0 = 1 → Engine Running (active)
  Bit 2 = 1 → Auto Mode (active)
  All other bits = 0 → no other alarms

Register 14 = 0x0000 → no alarms in word 2
```

**Critical alarms** (bits 3, 4, 5, 6) trigger automatic engine shutdown and require a Reset Alarms command to clear.

---

## Command Register (Register 20)

Write one of these values to register 20 to issue a command. The register is automatically cleared to 0 after the command is processed.

| Value | Command           | Description                                      |
|-------|-------------------|--------------------------------------------------|
| 0     | None              | No command / cleared                             |
| 1     | Start             | Begin engine start sequence (STOPPED → CRANKING)  |
| 2     | Stop              | Initiate cooldown and stop                       |
| 3     | Close Gen Breaker | Close generator output breaker (while RUNNING)   |
| 4     | Open Gen Breaker  | Open generator output breaker                    |
| 5     | Close Util Breaker| Close utility input breaker                      |
| 6     | Open Util Breaker | Open utility input breaker                       |
| 7     | E-Stop            | Emergency stop (immediate fault)                 |
| 8     | Reset Alarms      | Clear latched faults (FAULT → STOPPED)           |
| 9     | Toggle Auto       | Toggle automatic transfer mode on/off            |
| 10    | Utility Fail      | Simulate utility power failure                   |
| 11    | Restore Utility   | Restore utility power                            |
| 12    | Parallel Mode     | Switch to utility-parallel load sharing          |
| 13    | Island Mode       | Run generator-only with utility open             |
| 14    | Transfer Mode     | Restore mutual exclusion between breakers        |
| 20    | High Coolant Temp | Inject high coolant temperature fault            |
| 21    | Low Oil Pressure  | Inject low oil pressure fault                    |
| 22    | Overspeed         | Inject overspeed fault                           |
| 23    | Low Coolant Level | Inject low coolant level fault                   |
| 24    | Ground Fault      | Inject ground fault                              |
| 25    | Over Voltage      | Inject over voltage fault                        |
| 26    | Under Voltage     | Inject under voltage fault                       |
| 27    | High Battery V    | Inject high battery voltage fault                |
| 28    | Reverse Power     | Inject reverse power fault                       |
| 29    | Sync Check Fail   | Inject sync check fail fault                     |
| 30    | Load Imbalance    | Inject load imbalance fault                      |
| 31    | Fuel Leak         | Inject fuel leak fault                           |
| 32    | Air Filter Restricted | Inject air filter restriction fault         |
| 33    | Exhaust High Temp | Inject exhaust high temperature fault            |
| 34    | Gen Bearing Temp  | Inject generator bearing temperature fault       |
| 35    | Overcurrent       | Inject overcurrent fault                         |
| 36    | Loss of Field     | Inject loss of field fault                       |
| 37    | Utility Phase Loss| Inject utility phase loss fault                  |
| 38    | Parallel Sync Loss| Inject parallel sync loss fault                  |
| 39    | Setpoint Not Reached | Inject setpoint-not-reached fault             |

---

## Connecting with Third-Party Modbus Masters

### pymodbus (Python CLI)

```bash
pip install pymodbus

# Read all analog registers from GEN-01
python -c "
from pymodbus.client import ModbusTcpClient
client = ModbusTcpClient('localhost', port=5020)
client.connect()
result = client.read_holding_registers(0, 15, slave=1)
print('Registers:', result.registers)
client.close()
"

# Start GEN-01 via Modbus
python -c "
from pymodbus.client import ModbusTcpClient
client = ModbusTcpClient('localhost', port=5020)
client.connect()
client.write_register(20, 1, slave=1)  # Command 1 = Start
client.close()
"

# Read registers from GEN-05
python -c "
from pymodbus.client import ModbusTcpClient
client = ModbusTcpClient('localhost', port=5020)
client.connect()
result = client.read_holding_registers(0, 15, slave=5)
for i, val in enumerate(result.registers):
    print(f'  Register {i}: {val}')
client.close()
"
```

### ModRSsim2 / Modbus Poll (Windows GUI)

1. Open the application
2. Create a new TCP/IP connection:
   - **IP Address**: simulator host IP (for example `10.0.0.124`, or `127.0.0.1` from the same machine)
   - **Port**: `5020`
   - **Slave ID / Unit ID**: `1` (change to read other generators)
3. Set Function Code to **03 (Read Holding Registers)**
4. Set **Start Address**: `0`, **Quantity**: `15`
5. Click **Connect** / **Start Polling**
6. To send a command: write value to address `20` using FC 06

### QModMaster (Cross-Platform GUI)

1. Download from https://github.com/ed-chemnitz/qmodmaster
2. Connection settings:
   - **Type**: TCP
   - **Host**: simulator host IP (for example `10.0.0.124`, or `127.0.0.1` from the same machine)
   - **Port**: `5020`
   - **Unit ID**: `1`
3. Set **Function Code**: `03 Read Holding Registers`
4. **Start Address**: `0`, **Number**: `15`
5. Click **Read** to poll registers
6. To write a command: switch to FC 06, address `20`, write the command value

### Node-RED

1. Install `node-red-contrib-modbus`
2. Add a **Modbus-Read** node:
   - **Server**: Create new → TCP, Host simulator host IP, Port `5020`, Unit ID `1`
   - **FC**: `FC 3: Read Holding Registers`
   - **Address**: `0`
   - **Quantity**: `15`
   - **Poll Rate**: `1 second`
3. Connect to a **Debug** node to see values
4. To write commands: use a **Modbus-Write** node:
   - FC: `FC 6: Write Single Register`
   - Address: `20`
   - Inject the command value (1–11)

### Generic Modbus TCP Master

For any Modbus TCP master application:

1. **Connection**: TCP to `127.0.0.1:5020`
   Use the simulator machine's IP from another device, or `127.0.0.1:5020` from the same machine.
2. **Reading values**: Use FC 3 (Read Holding Registers)
   - Address range: 0–14 for analog values, 13–14 for alarm words
   - Unit ID: 1–255 on each port; use the fleet addressing table
3. **Writing commands**: Use FC 6 (Write Single Register)
   - Write to address 20 on the target unit ID
   - Value: 1–11 for operations, 20–27 for fault injection
4. **Polling**: 1-second interval recommended (matches simulation tick rate)

---

## Example Session

```
1. Connect to the simulator host IP on port 5020, Unit ID 1

2. Read registers 0-14:
   → [0, 3200, 1, 0, 920, 1234, 5678, 75, 0, 276, 0, 0, 0, 6, 0]
   Interpretation:
     Output kW:    0/10 = 0.0 kW (generator stopped)
     Utility Load: 3200/10 = 320.0 kW
     Util Breaker: 1 (closed)
     Gen Breaker:  0 (open)
     Fuel:         920/10 = 92.0%
     Run Hours:    1234 + 5678/10000 = 1234.5678 hrs
     Coolant:      75°F
     Oil Pressure: 0 PSI
     Battery:      276/10 = 27.6V
     RPM:          0
     Voltage:      0V
     Frequency:    0/10 = 0.0 Hz
     Alarm Word 1: 6 = 0b110 → Ready + Auto Mode
     Alarm Word 2: 0

3. Write register 20 = 1 (Start command)
   → Generator begins cranking

4. Wait 5 seconds, read registers again:
   → RPM = 1800, Voltage = 480, Frequency = 600 (60.0 Hz)
   → Alarm Word 1: 5 = 0b101 → Engine Running + Auto Mode

5. Write register 20 = 3 (Close Gen Breaker)
   → kW output ramps up, fuel level begins decreasing
```
