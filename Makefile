BUILD_DIR ?= .local/build
PYTHON_SOURCES := $(wildcard src/traffic_stats/*.py)

.PHONY: all clean
all:
	mkdir -p $(BUILD_DIR)/traffic_stats
	cp $(PYTHON_SOURCES) $(BUILD_DIR)/traffic_stats/
	cp src/traffic-statsd src/traffic-stats-rpc $(BUILD_DIR)/
	chmod 755 $(BUILD_DIR)/traffic-statsd $(BUILD_DIR)/traffic-stats-rpc

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD_DIR)

-include .local/tests/Makefile
