// Frida sniffer for Noi Solar app BLE traffic (flutter_blue_plus).
// Hooks CBPeripheral writes + the plugin's notification delegate.
function hex(u8) {
    var s = '';
    for (var i = 0; i < u8.length; i++) s += ('0' + u8[i].toString(16)).slice(-2);
    return s;
}

// 1) all outgoing writes
Interceptor.attach(ObjC.classes.CBPeripheral['- writeValue:forCharacteristic:type:'].implementation, {
    onEnter: function (args) {
        try {
            var data = new ObjC.Object(args[2]);
            var chr = new ObjC.Object(args[3]);
            var u8 = new Uint8Array(data.bytes().readByteArray(data.length()));
            console.log('[WRITE] ' + chr.UUID() + ' type=' + args[4] +
                        ' len=' + data.length() + ' data=' + hex(u8));
        } catch (e) { console.log('[WRITE] err ' + e); }
    }
});

// 2) incoming notifications via the flutter_blue_plus plugin delegate
var hooked = false;
for (var name in ObjC.classes) {
    if (/FlutterBluePlus/i.test(name)) {
        console.log('[*] found class ' + name);
        var cls = ObjC.classes[name];
        for (var selName of ['- peripheral:didUpdateValueForCharacteristic:error:']) {
            try {
                var m = cls[selName];
                if (m && !hooked) {
                    Interceptor.attach(m.implementation, {
                        onEnter: function (args) {
                            try {
                                var chr = new ObjC.Object(args[3]);
                                var val = chr.value();
                                if (val) {
                                    var u8 = new Uint8Array(val.bytes().readByteArray(val.length()));
                                    console.log('[NOTIFY] ' + chr.UUID() + ' data=' + hex(u8));
                                }
                            } catch (e) { console.log('[NOTIFY] err ' + e); }
                        }
                    });
                    hooked = true;
                    console.log('[*] hooked didUpdateValueForCharacteristic on ' + name);
                }
            } catch (e) {}
        }
    }
}
if (!hooked) console.log('[!] notify delegate NOT hooked (writes still captured)');

// 3) connection lifecycle
try {
    Interceptor.attach(ObjC.classes.CBCentralManager['- connectPeripheral:options:'].implementation, {
        onEnter: function (args) {
            var p = new ObjC.Object(args[2]);
            console.log('[CONNECT] ' + p.name());
        }
    });
} catch (e) {}

console.log('[*] sniffer ready');
