import mido
import threading

ports = ['SE25 MIDI1', 'SE25 MIDI2']

def listen(port_name):
    with mido.open_input(port_name) as inport:
        print(f'Listening on: {port_name}')
        for msg in inport:
            print(f'[{port_name}] {msg}')

threads = []
for port in ports:
    t = threading.Thread(target=listen, args=(port,), daemon=True)
    t.start()
    threads.append(t)

input("Press Enter to quit...\n")
