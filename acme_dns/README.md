# Home Assistant App: DNS-01 Let's Encrypt challenge server

acme-dns compatible server and account manager, providing DNS-01 challenge support for the
Let's Encrypt app without giving it access to your real DNS zone.

For DNS providers with no usable API (strato.de, among others): delegate one name to this app
instead of handing certbot credentials to your whole zone.

The Let's Encrypt app needs no modification — its built-in `dns-lego` / `acme-dns` provider
already speaks this protocol.

See [DOCS.md](DOCS.md) for setup.
