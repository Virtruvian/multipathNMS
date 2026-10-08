The localhost certificate and private key are public test fixtures, used only by
the loopback HTTPS tests. They must never be used for a deployment. The tests
explicitly trust this certificate for positive cases and verify that the default
system trust rejects it. Certificate validity is 2020–2036 to keep tests independent
of fixture creation time and small clock differences.
