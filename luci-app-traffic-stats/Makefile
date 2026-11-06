include $(TOPDIR)/rules.mk

PKG_VERSION:=1.0
PKG_RELEASE:=1
PKG_LICENSE:=GPL-3.0-only
LUCI_TITLE:=Per-device traffic statistics
LUCI_DEPENDS:=+luci-base +traffic-stats
LUCI_PKGARCH:=all

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
