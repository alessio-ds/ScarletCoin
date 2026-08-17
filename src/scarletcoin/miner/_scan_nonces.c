/* Native SHA-256 nonce scan for the ScarletCoin miner.
 *
 * Compiled on demand and loaded through ctypes; the pure-Python loop in
 * solver.py is the fallback when this is unavailable.
 */

#include <stdint.h>
#include <string.h>

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha256_compress(uint32_t state[8], const unsigned char block[64]) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)block[i * 4] << 24) | ((uint32_t)block[i * 4 + 1] << 16) |
               ((uint32_t)block[i * 4 + 2] << 8) | (uint32_t)block[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = state[0], b = state[1], c = state[2], d = state[3];
    uint32_t e = state[4], f = state[5], g = state[6], h = state[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t temp1 = h + S1 + ch + K[i] + w[i];
        uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = S0 + maj;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

static void sha256(const unsigned char *data, size_t len, unsigned char out[32]) {
    uint32_t state[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    };
    size_t offset = 0;
    unsigned char block[64];
    while (offset + 64 <= len) {
        memcpy(block, data + offset, 64);
        sha256_compress(state, block);
        offset += 64;
    }
    size_t rem = len - offset;
    memset(block, 0, 64);
    memcpy(block, data + offset, rem);
    block[rem] = 0x80;
    if (rem >= 56) {
        sha256_compress(state, block);
        memset(block, 0, 64);
    }
    uint64_t bits = (uint64_t)len * 8;
    for (int i = 0; i < 8; i++) block[63 - i] = (unsigned char)(bits >> (i * 8));
    sha256_compress(state, block);
    for (int i = 0; i < 8; i++) {
        out[i * 4] = (unsigned char)(state[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(state[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(state[i] >> 8);
        out[i * 4 + 3] = (unsigned char)(state[i]);
    }
}

/* Scan the nonce space of an 80-byte header. Returns the solved nonce, or -1.
 *
 * The header's nonce field (bytes 76..79) is overwritten on each iteration. A
 * hash solves the block when, read as a little-endian integer, it is <= target.
 */
long long scarlet_scan_nonces(const unsigned char *header, const unsigned char *target,
                              unsigned int start, unsigned int count) {
    unsigned char first[64];
    unsigned char block[64];
    unsigned char digest[32];
    unsigned char tail[16];
    uint32_t first_state[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    };

    /* Midstate: the state after the constant first 64 bytes of the header. */
    memcpy(first, header, 64);
    sha256_compress(first_state, first);

    /* Tail is the last 12 bytes (merkle tail + timestamp + bits) plus the nonce. */
    memcpy(tail, header + 64, 12);

    for (unsigned int i = 0; i < count; i++) {
        uint32_t nonce = start + i;
        tail[12] = (unsigned char)(nonce & 0xff);
        tail[13] = (unsigned char)((nonce >> 8) & 0xff);
        tail[14] = (unsigned char)((nonce >> 16) & 0xff);
        tail[15] = (unsigned char)((nonce >> 24) & 0xff);

        uint32_t state[8];
        memcpy(state, first_state, sizeof(state));
        memset(block, 0, 64);
        memcpy(block, tail, 16);
        block[16] = 0x80;
        uint64_t bits = 80ull * 8;
        for (int j = 0; j < 8; j++) block[63 - j] = (unsigned char)(bits >> (j * 8));
        sha256_compress(state, block);

        unsigned char inner[32];
        for (int j = 0; j < 8; j++) {
            inner[j * 4] = (unsigned char)(state[j] >> 24);
            inner[j * 4 + 1] = (unsigned char)(state[j] >> 16);
            inner[j * 4 + 2] = (unsigned char)(state[j] >> 8);
            inner[j * 4 + 3] = (unsigned char)(state[j]);
        }
        sha256(inner, 32, digest);

        /* Little-endian comparison against the target. */
        int ok = 1;
        for (int j = 31; j >= 0; j--) {
            if (digest[j] < target[j]) break;
            if (digest[j] > target[j]) { ok = 0; break; }
        }
        if (ok) return (long long)nonce;
    }
    return -1;
}
