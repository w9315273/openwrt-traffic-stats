include $(TOPDIR)/rules.mk

PKG_VERSION:=1.0.3
PKG_RELEASE:=2
PKG_LICENSE:=GPL-3.0-only
LUCI_TITLE:=Per-device traffic statistics
LUCI_DEPENDS:=+luci-base +traffic-stats
LUCI_PKGARCH:=all
# csstidy drops modern @supports rules used for theme colors and layout.
LUCI_MINIFY_CSS:=0

define Build/Prepare/luci-app-traffic-stats
	$(SED) 's/@TRAFFIC_STATS_VERSION@/$(PKG_VERSION)/g' $(PKG_BUILD_DIR)/htdocs/luci-static/resources/traffic-stats.js
endef

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
