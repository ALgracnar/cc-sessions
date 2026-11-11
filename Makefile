# cc-sessions
#
#   make install                 install into ~/.local  (no sudo)
#   sudo make install PREFIX=/usr   install system-wide
#   make uninstall              remove it again
#   make deb                    build a .deb package
#   make check                  syntax check + stress test
#
# The Python sources are installed verbatim into a libdir and reached through a
# generated launcher. That keeps the app's own imports untouched -- they work as
# plain sibling modules, which is what they were written and debugged as.

PREFIX     ?= $(HOME)/.local
DESTDIR    ?=

BINDIR      = $(PREFIX)/bin
LIBDIR      = $(PREFIX)/share/cc-sessions
DESKTOPDIR  = $(PREFIX)/share/applications
ICONDIR     = $(PREFIX)/share/icons/hicolor/scalable/apps

SOURCES     = cc-sessions.py roster.py session.py state.py stories.py theme.py

.PHONY: all install uninstall deb check clean

all:
	@echo "nothing to build -- pure Python. run 'make install'."

install:
	install -d $(DESTDIR)$(LIBDIR) $(DESTDIR)$(BINDIR) \
	           $(DESTDIR)$(DESKTOPDIR) $(DESTDIR)$(ICONDIR)
	install -m 644 $(SOURCES) $(DESTDIR)$(LIBDIR)/
	# Launcher, with the install location baked in.
	printf '%s\n' \
	  '#!/bin/sh' \
	  '# GSK_RENDERER=cairo avoids GTK4 GL renderer crashes inside Mesa.' \
	  '# The app sets this itself too; kept here so it is visible and overridable.' \
	  'GSK_RENDERER="$${GSK_RENDERER:-cairo}"' \
	  'export GSK_RENDERER' \
	  'exec python3 "$(LIBDIR)/cc-sessions.py" "$$@"' \
	  > $(DESTDIR)$(BINDIR)/cc-sessions
	chmod 755 $(DESTDIR)$(BINDIR)/cc-sessions
	install -m 755 cc-sessions-start $(DESTDIR)$(BINDIR)/cc-sessions-start
	sed 's|@BINDIR@|$(BINDIR)|g' data/cc-sessions.desktop.in \
	  > $(DESTDIR)$(DESKTOPDIR)/cc-sessions.desktop
	install -m 644 data/cc-sessions.svg $(DESTDIR)$(ICONDIR)/cc-sessions.svg
	-update-desktop-database $(DESTDIR)$(DESKTOPDIR) 2>/dev/null
	-gtk-update-icon-cache -f -t $(DESTDIR)$(PREFIX)/share/icons/hicolor 2>/dev/null
	@echo
	@echo "installed. run: cc-sessions   (or find 'Claude Sessions' in your apps)"

uninstall:
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(BINDIR)/cc-sessions $(DESTDIR)$(BINDIR)/cc-sessions-start
	rm -f $(DESTDIR)$(DESKTOPDIR)/cc-sessions.desktop
	rm -f $(DESTDIR)$(ICONDIR)/cc-sessions.svg
	-update-desktop-database $(DESTDIR)$(DESKTOPDIR) 2>/dev/null
	@echo "removed. your settings in ~/.config/cc-sessions were left alone."

deb:
	@command -v dpkg-buildpackage >/dev/null || { \
	  echo "need: sudo apt install debhelper devscripts"; exit 1; }
	dpkg-buildpackage -us -uc -b
	@echo
	@ls -1 ../cc-sessions_*.deb 2>/dev/null | tail -1

check:
	python3 -m py_compile $(SOURCES)
	@echo "  syntax ok"
	python3 stress_test.py 60

clean:
	rm -rf __pycache__ debian/cc-sessions debian/.debhelper debian/files \
	       debian/*.substvars debian/*.log debian/debhelper-build-stamp
