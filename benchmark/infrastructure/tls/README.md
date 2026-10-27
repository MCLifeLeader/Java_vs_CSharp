This self-signed certificate and its public test private key belong only to the
controller's loopback TLS echo fixture. The key is deliberately committed test
data, conveys no access to any real system, and must never be used for deployed
services. Both benchmark stacks explicitly trust this same certificate. TLS 1.3
is required; the negotiated cipher and session reuse policy remain run evidence.

Generated with OpenSSL on 2026-10-27 for `localhost` and `127.0.0.1`.
