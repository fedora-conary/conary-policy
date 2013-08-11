#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


VERSION=1.2
DESTDIR=/
POLICYDIR=/usr/lib/conary/policy/

all:

install:
	mkdir -p $(DESTDIR)$(POLICYDIR)
	install -m 644 policy/*.py $(DESTDIR)$(POLICYDIR)

dist:
	if ! grep "^Changes in $(VERSION)" NEWS > /dev/null 2>&1; then \
		echo "no NEWS entry"; \
		1; \
	fi
	$(MAKE) archive

archive:
	@rm -rf /tmp/conary-policy-$(VERSION) /tmp/conary-policy$(VERSION)-tmp
	@mkdir -p /tmp/conary-policy-$(VERSION)-tmp
	@git archive --format tar $(VERSION) | (cd /tmp/conary-policy-$(VERSION)-tmp/ ; tar x )
	@mv /tmp/conary-policy-$(VERSION)-tmp/ /tmp/conary-policy-$(VERSION)/
	@dir=$$PWD; cd /tmp; tar -c --bzip2 -f $$dir/conary-policy-$(VERSION).tar.bz2 conary-policy-$(VERSION)
	@rm -rf /tmp/conary-policy-$(VERSION)
	@echo "The archive is in conary-policy-$(VERSION).tar.bz2"

tag:
	git tag $(VERSION) refs/heads/master

version:
	sed -i 's/@NEW@/$(VERSION)/g' NEWS

show-version:
	@echo $(VERSION)

clean:
	rm -f policy/*.pyc
