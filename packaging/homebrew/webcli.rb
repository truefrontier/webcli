# Homebrew formula for truefrontier-webcli (installs the `webcli` and `llmcli` commands).
# Tap: truefrontier/homebrew-tap.  Install: brew install truefrontier/tap/webcli
# Playwright ships no sdist (wheel-only), so it is vendored as a macOS wheel resource below;
# webcli drives your installed Google Chrome (channel="chrome"), see caveats.

class Webcli < Formula
  include Language::Python::Virtualenv

  desc "Agent-native CLIs over your own signed-in, hidden Chrome sessions"
  homepage "https://github.com/truefrontier/webcli"
  url "https://files.pythonhosted.org/packages/cf/53/37d4e56538a14df7db8790bf030c501d149f188dc16bbb362ac69c129e4a/truefrontier_webcli-0.4.1.tar.gz"
  sha256 "9c1fd2face1706e10dc39cbac0a5b382d805af4378ed3e22a7d119e640d067bb"
  license "MIT"

  depends_on "python@3.13"

  # Playwright has no sdist on PyPI; vendor the arch-specific macOS wheel (its node driver is
  # bundled in the wheel, native per arch so `brew audit` stays happy, and no network is needed).
  on_macos do
    on_arm do
      resource "playwright" do
        url "https://files.pythonhosted.org/packages/b4/b3/99f6c07a7f59adb3830e42cda2e2093dc1c2cbd1f69fe4393ead30c88257/playwright-1.64.0-py3-none-macosx_11_0_arm64.whl"
        sha256 "8de42430e9a7c8b04ec963856d484d36ffd452882318ebad2df3e6fb49a8197a"
      end
    end
    on_intel do
      resource "playwright" do
        url "https://files.pythonhosted.org/packages/cc/b7/24e5c283694e7a63aa376ddde395031decd7d627c77b34a58953176e8a19/playwright-1.64.0-py3-none-macosx_10_13_x86_64.whl"
        sha256 "d76a501c9930b5a097b00e2448cda2200122a1e8e4be762ff535c1b076277737"
      end
    end
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/c7/0e/7fa0ef50764b67090eca4114772a2abf8b6148198475e54c660b97caeee6/click-8.5.0.tar.gz"
    sha256 "ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/49/2e/ced460408999b33da6b31b0021b0f37d329e202d4169aeb164493778f25b/pygments-2.21.0.tar.gz"
    sha256 "610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pyee" do
    url "https://files.pythonhosted.org/packages/1d/f1/fdedc2c75c3e31a330659c85e5793bb18b3397981fbf0844c6dee5b18926/pyee-14.0.0.tar.gz"
    sha256 "76dd0f4314ecd27f02dc73589dea7fd3853f9b6176d8ef9b122860657e3602de"
  end

  resource "greenlet" do
    url "https://files.pythonhosted.org/packages/3e/6e/0091f175ccd02b02bc8811bbcbcc6ac2e980be116e3b2f7a736ca322bf84/greenlet-3.5.6.tar.gz"
    sha256 "8e67c43bdfc88d5fee6db0d3e40175b362fc95fb85f0412d233b9b203c53a575"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  def install
    venv = virtualenv_create(libexec, "python3.13")
    # Playwright ships only platform wheels (no sdist); install everything else from source,
    # then install the vendored Playwright wheel directly (Homebrew's resource helper forces
    # --no-binary :all:, which cannot install a wheel).
    venv.pip_install resources.reject { |r| r.name == "playwright" }
    resource("playwright").stage do
      system libexec/"bin/python", "-m", "pip", "install", "--no-deps", "--no-index", Dir["*.whl"].first
    end
    venv.pip_install_and_link buildpath
  end

  def caveats
    <<~EOS
      webcli and llmcli drive your installed Google Chrome (Playwright channel="chrome").
      Install Google Chrome if you do not have it. This formula does NOT download Playwright's
      own browser binaries; if you want Playwright's bundled Chromium instead, run:
          #{opt_libexec}/bin/playwright install chromium

      Sign in once per identity (opens a VISIBLE Chrome window; run it yourself, not from an agent):
          webcli identity login google:default
          webcli identity login chatgpt:default
          webcli identity login meta:default
          webcli identity login linkedin:default
          webcli identity login facebook:default
          webcli identity login loom:default
          webcli identity login messages:default

      Data home is ~/.webcli (0700). See `webcli doctor`.
    EOS
  end

  test do
    assert_match "Data home", shell_output("#{bin}/webcli --help")
    assert_match "Chat AIs", shell_output("#{bin}/llmcli --help")
    system bin/"webcli", "sites"
  end
end
