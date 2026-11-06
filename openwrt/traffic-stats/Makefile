TRAFFIC_STATS_PACKAGE_DIR:=$(patsubst %/,%,$(dir $(realpath $(lastword $(MAKEFILE_LIST)))))
TRAFFIC_STATS_SOURCE_DIR?=$(realpath $(TRAFFIC_STATS_PACKAGE_DIR)/../..)

include $(TOPDIR)/rules.mk

PKG_NAME:=traffic-stats
PKG_VERSION:=1.0
PKG_RELEASE:=2
PKG_LICENSE:=GPL-3.0-only
PKG_FILE_DEPENDS:=$(TRAFFIC_STATS_SOURCE_DIR)/src $(TRAFFIC_STATS_SOURCE_DIR)/Makefile

include $(INCLUDE_DIR)/package.mk

define Package/traffic-stats
  SECTION:=net
  CATEGORY:=Network
  TITLE:=Per-device traffic accounting with nftables
  PKGARCH:=all
  DEPENDS:=+python3-light +python3-sqlite3 +rpcd +ubus +uci +nftables-json
endef

define Package/traffic-stats/description
  Account for LAN to WAN IPv4 and IPv6 traffic by device, with SQLite
  history, persistent sampling checkpoints, a procd-managed collector
  and bounded queries through a Python rpcd executable plugin.
endef

define Package/traffic-stats/conffiles
/etc/config/traffic-stats
endef

define Build/Prepare
	test -f "$(TRAFFIC_STATS_SOURCE_DIR)/src/traffic-statsd"
	$(INSTALL_DIR) $(PKG_BUILD_DIR)/src/traffic_stats
	$(CP) $(TRAFFIC_STATS_SOURCE_DIR)/src/traffic_stats/*.py $(PKG_BUILD_DIR)/src/traffic_stats/
	$(CP) $(TRAFFIC_STATS_SOURCE_DIR)/src/traffic-statsd $(TRAFFIC_STATS_SOURCE_DIR)/src/traffic-stats-rpc $(PKG_BUILD_DIR)/src/
	$(CP) $(TRAFFIC_STATS_SOURCE_DIR)/Makefile $(PKG_BUILD_DIR)/Makefile
endef

define Build/Configure
endef

define Build/Compile
	$(MAKE) -C $(PKG_BUILD_DIR) BUILD_DIR=build all
endef

define Package/traffic-stats/install
	$(INSTALL_DIR) $(1)/usr/sbin $(1)/usr/libexec/rpcd $(1)/etc/config $(1)/etc/init.d $(1)/lib/upgrade/keep.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/build/traffic-statsd $(1)/usr/sbin/traffic-statsd
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/build/traffic-stats-rpc $(1)/usr/libexec/rpcd/traffic-stats
	$(INSTALL_DIR) $(1)/usr/lib/traffic-stats/traffic_stats
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/build/traffic_stats/*.py $(1)/usr/lib/traffic-stats/traffic_stats/
	$(INSTALL_CONF) ./files/traffic-stats.config $(1)/etc/config/traffic-stats
	$(INSTALL_BIN) ./files/traffic-stats.init $(1)/etc/init.d/traffic-stats
	$(INSTALL_DATA) ./files/traffic-stats.keep $(1)/lib/upgrade/keep.d/traffic-stats
endef

$(eval $(call BuildPackage,traffic-stats))
