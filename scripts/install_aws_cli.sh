#!/usr/bin/env bash
# Install the reviewed AWS CLI v2 release on the Ubuntu 24.04 ARM64 host before runtime installation.
# The release signature must verify against AWS's published CLI signing key, whose fingerprint is pinned below.
set -euo pipefail
umask 022

version=2.37.7
fingerprint=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
url="https://awscli.amazonaws.com/awscli-exe-linux-aarch64-${version}.zip"

[[ "$(id -u)" == 0 ]] || {
	printf 'AWS CLI installation requires root on the reviewed host.\n' >&2
	exit 1
}
[[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || {
	printf 'This installer supports Linux ARM64 only.\n' >&2
	exit 1
}
if command -v aws >/dev/null && [[ "$(aws --version)" == "aws-cli/${version} "* ]]; then
	aws --version
	exit 0
fi

export DEBIAN_FRONTEND=noninteractive
timeout --kill-after=10s 600s apt-get -o DPkg::Lock::Timeout=60 install --yes --no-install-recommends unzip gnupg curl >/dev/null
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsS --max-time 300 -o "$work/awscliv2.zip" "$url"
curl -fsS --max-time 60 -o "$work/awscliv2.sig" "$url.sig"
export GNUPGHOME="$work/gnupg"
install -d -m 0700 "$GNUPGHOME"
gpg --batch --quiet --import <<'KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
KEY
gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${fingerprint}:" || {
	printf 'Imported AWS CLI signing key fingerprint does not match the pinned value.\n' >&2
	exit 1
}
gpg --batch --status-fd 1 --verify "$work/awscliv2.sig" "$work/awscliv2.zip" 2>/dev/null | grep -q "^\[GNUPG:\] VALIDSIG ${fingerprint} " || {
	printf 'AWS CLI release signature verification failed; nothing was installed.\n' >&2
	exit 1
}
unzip -q "$work/awscliv2.zip" -d "$work"
"$work/aws/install" --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update
aws --version
