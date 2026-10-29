# Development package for the audited fork. Original project: Michal Krenek.
pkgname=soapy_power-git
pkgver=1.12.2.r0
pkgrel=1
pkgdesc="Power spectrum measurement from SoapySDR devices (audited development fork)"
arch=('any')
url="https://github.com/djayuffe/soapy_power"
license=('MIT')
depends=('python' 'python-numpy' 'simplesoapy>=1.5.0' 'simplespectral')
makedepends=('git' 'python-setuptools')
provides=('soapy_power')
conflicts=('soapy_power')
optdepends=(
  'python-pyfftw: optional FFTW backend'
  'python-scipy: optional SciPy FFT backend'
)
source=('soapy_power::git+https://github.com/djayuffe/soapy_power.git#branch=master')
sha256sums=('SKIP')

pkgver() {
  cd "$srcdir/soapy_power"
  printf '1.12.2.r%s.g%s' "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

build() {
  cd "$srcdir/soapy_power"
  python setup.py build
}

package() {
  cd "$srcdir/soapy_power"
  python setup.py install --root="$pkgdir"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
