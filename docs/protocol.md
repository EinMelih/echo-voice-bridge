# Protocol notes

What the bridge relies on, read from the firmware source (`src/hassmic/arb.c`, `dropin.c`) at `v2026.11.05.000019-beta`. Integers are little endian.

## Echo network (UDP 28930)

| Packet | Layout |
|---|---|
| beacon | `"HMA1" 1 pub[32] net[8] nlen node [ctr[8] tag[16]]` |
| claim | `"HMA1" 4 net[8] id[8] ctr[8] kw[8] score[4] prio won tag[16]` |
| message | `"HMA1" 7 net[8] from[8] to[8] ctr[8] len payload[len] tag[16]` |

- `id` is the first 8 bytes of the public key. `kw` is the BLAKE2b hash (8 bytes) of the lower-case wake word.
- `tag` is keyed BLAKE2b (16 bytes) with a key derived from the network key K (`"hassmic arbitration mac"`).
- Each peer's counter must rise. **A packet with a lower counter is dropped**, which is why the first copy of a claim must leave before any later message is built: an invite that overtakes the claim makes the Echo reject the claim and start its own pipeline too.

### Joining

Home Assistant's `esphome.<node>_arbitration_key` action hands an Echo a network: `network` is the id in hex, `key` is base64 of `sender_pub[32] nonce[24] mac[16] enc(K)[32]`. An Echo only moves to a network with a lower id.

The wrapping key is keyed BLAKE2b over `"hassmic arb key\0" sender recipient net`, with the X25519 shared secret as the key. The cipher is XChaCha20-Poly1305 with the sender's public key as additional data.

### Winning a round

The Echo claims a wake word and waits for other members, for up to its learned window. A claim with `won=1` ends the round at once and the Echo stays silent. A claim with a low score and `won=0` tells it to go ahead without waiting.

## Drop In (UDP 28932)

Signalling goes in network messages: `kind call_id[8] port[2] epk[32] len reason[len]`, with kinds invite 1, accept 2, ringing 3, refuse 4, bye 5.

The call key is keyed BLAKE2b over `shared call_id caller_id callee_id`, with the key derived from K (`"hassmic drop in 1"`).

Audio: `"HMD1" call_id[8] dir seq[4] enc(opus) mac[16]`, XChaCha20-Poly1305, nonce `call_id dir seq` padded to 24 bytes, header as additional data. `dir` is 0 from the caller and 1 from the callee. Opus at 16 kHz mono in 20 ms frames. Every frame is sent, silence included: 5 s without one ends the call.

## The microphone gate

For each frame the firmware works out:

- **far end talking:** the level held from what it played is above −55 dBFS and more than 25 dB over `far_floor`.
- `far_floor` follows the quietest played frame at once, and otherwise rises by 1.5 dB/s. It starts each call at −70 dBFS.
- **near end talking:** the mic is 9 dB over the room floor and above −58 dBFS, and not attributed to echo.

If the near end is not talking and the far end is, or the hang time is over, the mic is lowered by 25 dB. Holding a −36 dBFS floor and limiting speech to −12 dBFS keeps the far end from counting as talking, which follows from these rules. It takes about 22 s from the start of a call for `far_floor` to climb there.
