#include <stddef.h>
#include <stdint.h>
#if defined(_WIN32)
#define BENCH_EXPORT __declspec(dllexport)
/* No CRT is required by either function. Project-local builds may use this entry. */
int bench_dll_entry(void *instance, unsigned int reason, void *reserved) {
    (void)instance; (void)reason; (void)reserved; return 1;
}
#else
#define BENCH_EXPORT __attribute__((visibility("default")))
#endif

/* Shared C ABI; unsigned wrapping sum has defined modulo-2^64 semantics. */
BENCH_EXPORT uint64_t bench_sum(const int64_t *values, size_t count) {
    uint64_t sum = 0;
    for (size_t i = 0; i < count; i++) sum += (uint64_t)values[i];
    return sum;
}

BENCH_EXPORT uint64_t bench_buffer(const uint8_t *bytes, size_t count) {
    uint64_t sum = 0;
    for (size_t i = 0; i < count; i++) sum += bytes[i];
    return sum;
}
