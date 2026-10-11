# Changelog

## [1.7.0](https://github.com/OpenEngine/OpenEngine/compare/v1.6.0...v1.7.0) (2026-10-11)


### Features

* **graph-service:** show a loop's latest run output ([#785](https://github.com/OpenEngine/OpenEngine/issues/785)) ([a7a3895](https://github.com/OpenEngine/OpenEngine/commit/a7a38955bde21340222d6f4fad4953b02c47d203))


### Documentation

* **site:** publish the graphs and loops post on the blog ([#794](https://github.com/OpenEngine/OpenEngine/issues/794)) ([8d71fb2](https://github.com/OpenEngine/OpenEngine/commit/8d71fb212a57f8308c1a566d2ed15791455fa0b4))

## [1.6.0](https://github.com/OpenEngine/OpenEngine/compare/v1.5.0...v1.6.0) (2026-10-09)


### ⚠ BREAKING CHANGES

* **cli:** list commands print a table unless --json is given.

### Features

* **cli:** print tables by default for list commands, add --json ([#724](https://github.com/OpenEngine/OpenEngine/issues/724)) ([b88e338](https://github.com/OpenEngine/OpenEngine/commit/b88e338567b51b569292b4a506e63e39578f00ef))
* **site:** add a GitHub link to the navbar ([#791](https://github.com/OpenEngine/OpenEngine/issues/791)) ([af823a1](https://github.com/OpenEngine/OpenEngine/commit/af823a1f62ff18100daa289a7741b9727d086c30))

## [1.5.0](https://github.com/OpenEngine/OpenEngine/compare/v1.4.0...v1.5.0) (2026-10-09)


### Features

* **cli:** add built-in adversarial-review graph ([#723](https://github.com/OpenEngine/OpenEngine/issues/723)) ([8095b47](https://github.com/OpenEngine/OpenEngine/commit/8095b47c659fc84e42e61311795cda69df359dfa))
* record and show when workorders and graph nodes start ([#721](https://github.com/OpenEngine/OpenEngine/issues/721)) ([ff1ce26](https://github.com/OpenEngine/OpenEngine/commit/ff1ce265399abe6cb12a45077f489fc0292bb312))


### Bug Fixes

* graph runs tolerate unloadable versions ([#783](https://github.com/OpenEngine/OpenEngine/issues/783)) ([3871556](https://github.com/OpenEngine/OpenEngine/commit/38715561204ce5bb547100406ce2971e9749b714))


### Documentation

* **site:** replace workflows/workorders with graphs and loops, add CLI reference ([#786](https://github.com/OpenEngine/OpenEngine/issues/786)) ([a713171](https://github.com/OpenEngine/OpenEngine/commit/a7131715674328eeb31ea15f9fd34abc31255db7))
* **skills:** add a skill for managing graphs ([#784](https://github.com/OpenEngine/OpenEngine/issues/784)) ([17c982b](https://github.com/OpenEngine/OpenEngine/commit/17c982b338d45683ab8c06edd885122ad62e2851))

## [1.4.0](https://github.com/OpenEngine/OpenEngine/compare/v1.3.0...v1.4.0) (2026-10-09)


### Features

* agents, and connections ([#714](https://github.com/OpenEngine/OpenEngine/issues/714)) ([6eda259](https://github.com/OpenEngine/OpenEngine/commit/6eda259d17c342a3dc9ae8b3e20cc493ead2210d))
* cli commands for graph and loop management ([564e1e6](https://github.com/OpenEngine/OpenEngine/commit/564e1e66a942f4f2d4449e2455edc1b6ce25dde8))
* **cli:** add engine agent claude interactive sessions ([#716](https://github.com/OpenEngine/OpenEngine/issues/716)) ([2fdd39e](https://github.com/OpenEngine/OpenEngine/commit/2fdd39e5895e44df2160ca9fc1266afba4c869fe))
* **cli:** add graph and loop specification commands ([#703](https://github.com/OpenEngine/OpenEngine/issues/703)) ([a191c39](https://github.com/OpenEngine/OpenEngine/commit/a191c392a095124d8b2f78da4527649ee8c266df))
* **cli:** rename graph execute to graph run and add engine runs ([#713](https://github.com/OpenEngine/OpenEngine/issues/713)) ([c1688e3](https://github.com/OpenEngine/OpenEngine/commit/c1688e3ad6c43bc39f129408302c2434fcda6ac3))
* **github:** accept webhooks from multiple repositories ([#711](https://github.com/OpenEngine/OpenEngine/issues/711)) ([a4879bd](https://github.com/OpenEngine/OpenEngine/commit/a4879bdb8df919e0738507e10cd46371979bec21))
* **github:** react to Engine mentions with delivery outcomes ([#661](https://github.com/OpenEngine/OpenEngine/issues/661)) ([ccaa260](https://github.com/OpenEngine/OpenEngine/commit/ccaa2604fd9cd9b337f8b2839809ad6a9fba0551))
* link issue commits and resolve addressed review threads ([#658](https://github.com/OpenEngine/OpenEngine/issues/658)) ([88bd7f3](https://github.com/OpenEngine/OpenEngine/commit/88bd7f36ccb1e4d18b4893cf3f808f5f9974e130))
* **mcp:** add tools to create loops and check their status ([#638](https://github.com/OpenEngine/OpenEngine/issues/638)) ([b8d6c53](https://github.com/OpenEngine/OpenEngine/commit/b8d6c535f803a33e650249d6cfac0a1692c42605))
* **open-verify:** add agent-powered CLI for browser and terminal QA ([#598](https://github.com/OpenEngine/OpenEngine/issues/598)) ([65dec07](https://github.com/OpenEngine/OpenEngine/commit/65dec0726aef6dd20c1dc6ed92c0c5425fd7fc08))
* **ports:** add sandbox contract and process backend ([#691](https://github.com/OpenEngine/OpenEngine/issues/691)) ([b0d1f48](https://github.com/OpenEngine/OpenEngine/commit/b0d1f48e785be37f25432c72afef0924b89dfe80))
* post findings and impact analysis when Engine is requested as reviewer ([#695](https://github.com/OpenEngine/OpenEngine/issues/695)) ([9882112](https://github.com/OpenEngine/OpenEngine/commit/988211251c247abd8a762f7935899afe1f6dc2d2))
* **repos:** support optional local repository onboarding ([#712](https://github.com/OpenEngine/OpenEngine/issues/712)) ([9ba6a52](https://github.com/OpenEngine/OpenEngine/commit/9ba6a52ada33557b90ea97d712e7acfe9cfa1097))


### Bug Fixes

* **cli:** heal daemon Node version-manager shims ([#654](https://github.com/OpenEngine/OpenEngine/issues/654)) ([ee94c23](https://github.com/OpenEngine/OpenEngine/commit/ee94c231a45f81e9acbfdf2473e9af0d7d3a06ef))
* **cli:** preserve daemon identity and configure Claude account ([#656](https://github.com/OpenEngine/OpenEngine/issues/656)) ([dcd5695](https://github.com/OpenEngine/OpenEngine/commit/dcd5695772e24c6b59626f5bd9dbaa0a0025ca65))
* ignore GitHub comments mentioning other users ([#659](https://github.com/OpenEngine/OpenEngine/issues/659)) ([49b2ab9](https://github.com/OpenEngine/OpenEngine/commit/49b2ab9e5e0541ae3582f124b590527647075871))
* **web:** log every GitHub delivery the ingress settles without acting ([#704](https://github.com/OpenEngine/OpenEngine/issues/704)) ([6046dcf](https://github.com/OpenEngine/OpenEngine/commit/6046dcf230670bba0bb6592adb4e50347ade75be))


### Documentation

* add Windows installation command to README ([#702](https://github.com/OpenEngine/OpenEngine/issues/702)) ([a1e22d0](https://github.com/OpenEngine/OpenEngine/commit/a1e22d0f313f910bc11c0ae507873972fdb60e73))
* **site:** enable blog and add "AI Skeptic to AI Pilled" post ([#641](https://github.com/OpenEngine/OpenEngine/issues/641)) ([c077bf2](https://github.com/OpenEngine/OpenEngine/commit/c077bf2b7222cfdaf5668aaa2740d48f31e0af04))

## [1.3.0](https://github.com/OpenEngine/OpenEngine/compare/v1.2.0...v1.3.0) (2026-10-02)


### Features

* add a script to install the checked-out commit locally ([#633](https://github.com/OpenEngine/OpenEngine/issues/633)) ([f5c0c07](https://github.com/OpenEngine/OpenEngine/commit/f5c0c0749a6183285d55acb3192319ab4317a29d))
* **web:** add a Loops section to the sidebar for loop settings ([#629](https://github.com/OpenEngine/OpenEngine/issues/629)) ([7c5e0f8](https://github.com/OpenEngine/OpenEngine/commit/7c5e0f8e7a7e9eb9c57b7811256ff8b5bf2d6034))
* **web:** create and run loops with the concierge's WorkOrder tools ([#635](https://github.com/OpenEngine/OpenEngine/issues/635)) ([ea2974f](https://github.com/OpenEngine/OpenEngine/commit/ea2974f413fa3b3d3962f7d119a3c396e95a5aa2))


### Bug Fixes

* **cli:** keep the review spinner on one terminal line ([#634](https://github.com/OpenEngine/OpenEngine/issues/634)) ([790e073](https://github.com/OpenEngine/OpenEngine/commit/790e0730c6040b72d41bf45bb6c01164d99d6204))
* **cli:** show each review finding below its diff ([#637](https://github.com/OpenEngine/OpenEngine/issues/637)) ([aad5dce](https://github.com/OpenEngine/OpenEngine/commit/aad5dceb9e31047f2fa6984b1b483bfa3eaeef81))
* dev server would fail to start ootb due to github integration ([#631](https://github.com/OpenEngine/OpenEngine/issues/631)) ([ffdb7cd](https://github.com/OpenEngine/OpenEngine/commit/ffdb7cdaa1100775dcef291b0ad4b3612e7cb9f2))
* **runtime:** let a step complete without posting a pull-request comment ([#636](https://github.com/OpenEngine/OpenEngine/issues/636)) ([6a852d7](https://github.com/OpenEngine/OpenEngine/commit/6a852d7d19b73bc7ac46466bdefbebbf468969eb))


### Documentation

* add Slack and documentation badges to README ([#628](https://github.com/OpenEngine/OpenEngine/issues/628)) ([f1cd9c1](https://github.com/OpenEngine/OpenEngine/commit/f1cd9c1f72e808d109446a61cba480659782ec7b))

## [1.2.0](https://github.com/OpenEngine/OpenEngine/compare/v1.1.0...v1.2.0) (2026-10-01)


### Features

* **cli:** report each step of engine review as it starts and finishes ([#625](https://github.com/OpenEngine/OpenEngine/issues/625)) ([97e88a9](https://github.com/OpenEngine/OpenEngine/commit/97e88a931518d8de5dffe641b3e66e831d79559c))
* triage review findings individually with highlighted diffs ([#627](https://github.com/OpenEngine/OpenEngine/issues/627)) ([0cd5dfd](https://github.com/OpenEngine/OpenEngine/commit/0cd5dfd55be6befb61facf9833cf806fe0416201))
* **web:** log what becomes of each GitHub delivery and warn when one stalls the queue ([#621](https://github.com/OpenEngine/OpenEngine/issues/621)) ([c40be15](https://github.com/OpenEngine/OpenEngine/commit/c40be15a7267e8620a8accec1d8db501a31ed7d6))

## [1.1.0](https://github.com/OpenEngine/OpenEngine/compare/v1.0.0...v1.1.0) (2026-10-01)


### Features

* **cli:** add engine review to start the workflow in review and triage findings ([#604](https://github.com/OpenEngine/OpenEngine/issues/604)) ([f29d9d8](https://github.com/OpenEngine/OpenEngine/commit/f29d9d8e9a72910c1dcefb3ab5b1863877a5c6ec))
* scope work order visibility to the repositories a user can write to ([#590](https://github.com/OpenEngine/OpenEngine/issues/590)) ([3ca4374](https://github.com/OpenEngine/OpenEngine/commit/3ca43742ee38ada44846202c8a6eb8250d0d0f98))
* **web:** start an engine review when Engine is requested as a pull request reviewer ([#623](https://github.com/OpenEngine/OpenEngine/issues/623)) ([d79821f](https://github.com/OpenEngine/OpenEngine/commit/d79821feab6ee92aa871287c00762dc46cafe35c))


### Bug Fixes

* **release:** stop pinning release-please to 1.0.0 ([#622](https://github.com/OpenEngine/OpenEngine/issues/622)) ([4296cf2](https://github.com/OpenEngine/OpenEngine/commit/4296cf28aebdbe83a8f8e0d87a6c12cfc33b5350))


### Documentation

* **site:** feature the 1.0 release video on the homepage ([#619](https://github.com/OpenEngine/OpenEngine/issues/619)) ([e35c8d1](https://github.com/OpenEngine/OpenEngine/commit/e35c8d15fb4cd2d134e73b1276ffc4017f7374e7))

## [1.0.0](https://github.com/OpenEngine/OpenEngine/compare/v0.7.0...v1.0.0) (2026-09-30)


### Bug Fixes

* **web:** confirm access with the user's own token when the server's GitHub connection fails ([#612](https://github.com/OpenEngine/OpenEngine/issues/612)) ([73ee0e8](https://github.com/OpenEngine/OpenEngine/commit/73ee0e8aecf7d84414bca699b72cc66f555e933c))


### Miscellaneous Chores

* release 1.0.0 ([#615](https://github.com/OpenEngine/OpenEngine/issues/615)) ([c2376a4](https://github.com/OpenEngine/OpenEngine/commit/c2376a42087403f1de30fe21e0d513a4f7396068))

## [0.7.0](https://github.com/OpenEngine/OpenEngine/compare/v0.6.0...v0.7.0) (2026-09-29)


### Features

* **cli:** ask for the approval mode in engine init and use the GitHub OAuth token for agents ([#606](https://github.com/OpenEngine/OpenEngine/issues/606)) ([b0c799f](https://github.com/OpenEngine/OpenEngine/commit/b0c799f0a31a7cd5c7e7918390ebf9cb6b25969a))


### Documentation

* **site:** rewrite MCP integration page around connecting a client ([#607](https://github.com/OpenEngine/OpenEngine/issues/607)) ([896aaf0](https://github.com/OpenEngine/OpenEngine/commit/896aaf0eac0eb74d0673e4fdc141c937bbb7a394))

## [0.6.0](https://github.com/OpenEngine/OpenEngine/compare/v0.5.0...v0.6.0) (2026-09-29)


### Features

* **cli:** choose connected or disconnected mode in engine init ([#605](https://github.com/OpenEngine/OpenEngine/issues/605)) ([7ea27d6](https://github.com/OpenEngine/OpenEngine/commit/7ea27d6397f2e69be0327c0394b00f9c03c5d9c3))


### Bug Fixes

* **web:** unnest sidebar node groups holding a single conversation ([#602](https://github.com/OpenEngine/OpenEngine/issues/602)) ([17b490e](https://github.com/OpenEngine/OpenEngine/commit/17b490e2b3b858a6e81cb1042a44358d891faa63))

## [0.5.0](https://github.com/OpenEngine/OpenEngine/compare/v0.4.0...v0.5.0) (2026-09-29)


### Features

* add a disconnected mode for people with no integrations ([#585](https://github.com/OpenEngine/OpenEngine/issues/585)) ([936c136](https://github.com/OpenEngine/OpenEngine/commit/936c136b5afeb6b53cd5a49575bfb4a6e2ee8796))
* **cli:** add engine init to onboard the current repository ([#601](https://github.com/OpenEngine/OpenEngine/issues/601)) ([409e1d8](https://github.com/OpenEngine/OpenEngine/commit/409e1d852fce462083d5cc04e6a3e22c1b3bf086))
* **mcp:** add workorder status and node steering tools ([#587](https://github.com/OpenEngine/OpenEngine/issues/587)) ([1112727](https://github.com/OpenEngine/OpenEngine/commit/1112727c02749bcd928e27ec3d6af5c1b482994e))
* **web:** link the pull request in the WorkOrder overview ([#558](https://github.com/OpenEngine/OpenEngine/issues/558)) ([dbd905c](https://github.com/OpenEngine/OpenEngine/commit/dbd905c0c3a50ed515f5ba0db663478fe2b348e3))


### Bug Fixes

* explain agent rate limits after account switches ([#556](https://github.com/OpenEngine/OpenEngine/issues/556)) ([4ea45b1](https://github.com/OpenEngine/OpenEngine/commit/4ea45b1fffbe0f8c2f32e7267e29c9b83ee6b5bc))
* improve Slack task output and status presentation ([#595](https://github.com/OpenEngine/OpenEngine/issues/595)) ([c945e18](https://github.com/OpenEngine/OpenEngine/commit/c945e188e1d03a81ad81a7c21b9ed6af23de88ee))
* start GitHub work orders in the repository's local checkout ([#597](https://github.com/OpenEngine/OpenEngine/issues/597)) ([bc3bc8b](https://github.com/OpenEngine/OpenEngine/commit/bc3bc8bcd551525baa699ee81df1a37e2d631cf2))
* wait for launchd teardown before restarting daemon ([#599](https://github.com/OpenEngine/OpenEngine/issues/599)) ([0e331fb](https://github.com/OpenEngine/OpenEngine/commit/0e331fb1863b5e221cacf8292c9a10039412fdb6))


### Documentation

* add OpenEngine installation quickstart ([#594](https://github.com/OpenEngine/OpenEngine/issues/594)) ([0b9e6e2](https://github.com/OpenEngine/OpenEngine/commit/0b9e6e245dab117ee2b7d4748ddf524a3dac629f))
* **mcp:** add integration guide to docsite ([#593](https://github.com/OpenEngine/OpenEngine/issues/593)) ([05f4acc](https://github.com/OpenEngine/OpenEngine/commit/05f4acc4655d1810f98fb5a10f23a6e080c9ec57))
* simplify quickstart and README install steps ([#596](https://github.com/OpenEngine/OpenEngine/issues/596)) ([703d6be](https://github.com/OpenEngine/OpenEngine/commit/703d6be497ff8bd19e7055b0e4d12cba603d0802))

## [0.4.0](https://github.com/OpenEngine/OpenEngine/compare/v0.3.0...v0.4.0) (2026-09-28)


### Features

* add an OpenCode ACP runner ([#561](https://github.com/OpenEngine/OpenEngine/issues/561)) ([7e2b62b](https://github.com/OpenEngine/OpenEngine/commit/7e2b62bd1800be214c3a299a62a87f10701d3436))
* **cli:** improve interactive work order experience ([#551](https://github.com/OpenEngine/OpenEngine/issues/551)) ([07171f2](https://github.com/OpenEngine/OpenEngine/commit/07171f2df1c032e27e8f1ef8df8dcd0f13cfdb23))
* post WorkOrder progress as comments on the originating GitHub issue ([#559](https://github.com/OpenEngine/OpenEngine/issues/559)) ([8aa9da9](https://github.com/OpenEngine/OpenEngine/commit/8aa9da9eca5e69b8a0102afbbdf1c3a007124932))


### Documentation

* propose Ongoing Projects on idle Resources ([#560](https://github.com/OpenEngine/OpenEngine/issues/560)) ([d0d7a8a](https://github.com/OpenEngine/OpenEngine/commit/d0d7a8ac6486811adfe1771d0b0563dd138a404e))

## [0.3.0](https://github.com/OpenEngine/OpenEngine/compare/v0.2.0...v0.3.0) (2026-09-25)


### Features

* add one-line installer for macOS and Linux ([#553](https://github.com/OpenEngine/OpenEngine/issues/553)) ([bd798b6](https://github.com/OpenEngine/OpenEngine/commit/bd798b6528787640e1633795023c0cde5d8a99d0))
* credit the work order's requester as co-author on agent commits ([#526](https://github.com/OpenEngine/OpenEngine/issues/526)) ([c4d28a9](https://github.com/OpenEngine/OpenEngine/commit/c4d28a9bed9cbfe14dfeeb6e842a2d8f0a4779e8))
* run OpenEngine as a background service with engine daemon ([#555](https://github.com/OpenEngine/OpenEngine/issues/555)) ([0f1c56c](https://github.com/OpenEngine/OpenEngine/commit/0f1c56ccbe206512c907d9492ce81262d582427a))


### Bug Fixes

* replace openengine launcher with engine ([#557](https://github.com/OpenEngine/OpenEngine/issues/557)) ([aba6780](https://github.com/OpenEngine/OpenEngine/commit/aba678059129fad24aba3a22c2bdc2aaaf6a0472))

## [0.2.0](https://github.com/OpenEngine/OpenEngine/compare/v0.1.0...v0.2.0) (2026-09-25)


### Features

* admit operators and any-repository writers, and recheck access ([#541](https://github.com/OpenEngine/OpenEngine/issues/541)) ([c27d00e](https://github.com/OpenEngine/OpenEngine/commit/c27d00e041db5b1f879eb2aa5ce236d24f60ae21))
* measure node usage in approximate dollars and roll it up to the WorkOrder ([#550](https://github.com/OpenEngine/OpenEngine/issues/550)) ([7718fa7](https://github.com/OpenEngine/OpenEngine/commit/7718fa7b141c9d723fcc2324b4ae0f571482637d))
* run OpenEngine outside a source checkout ([#549](https://github.com/OpenEngine/OpenEngine/issues/549)) ([19ff3a9](https://github.com/OpenEngine/OpenEngine/commit/19ff3a9d0162dafc845b85fd65e9fe98a0d1c0cd))

## 0.1.0 (2026-09-24)


### ⚠ BREAKING CHANGES

* engine-adapter-agent-runner-codex and engine-adapter-agent-runner-claude-code are removed, as is ENGINE_AGENT_PROTOCOL_LOG.
* remove workstreams from the planning hierarchy ([#402](https://github.com/OpenEngine/OpenEngine/issues/402))

### Features

* **acp:** wire engine.toml attribution and output_style to Claude ACP sessions ([#324](https://github.com/OpenEngine/OpenEngine/issues/324)) ([d787497](https://github.com/OpenEngine/OpenEngine/commit/d787497c8abc0f96df380851f2661199d9503166))
* add configured repository dropdown for workorders ([#477](https://github.com/OpenEngine/OpenEngine/issues/477)) ([7c1c1b3](https://github.com/OpenEngine/OpenEngine/commit/7c1c1b356f8fe61f205eb62cf94aeb4d1be0ca48))
* add dependabot config for supply chain monitoring ([#215](https://github.com/OpenEngine/OpenEngine/issues/215)) ([dbf92ae](https://github.com/OpenEngine/OpenEngine/commit/dbf92ae5611feb69f58e5a32171ac0150a7e3b39))
* Add disclosure caret and progress sub-nodes to review groups ([#363](https://github.com/OpenEngine/OpenEngine/issues/363)) ([fe74a8d](https://github.com/OpenEngine/OpenEngine/commit/fe74a8d8ad24ab8e1ae30e13b2250511eed18443))
* add DRYness and code duplication reviewer ([#484](https://github.com/OpenEngine/OpenEngine/issues/484)) ([2b4d8dc](https://github.com/OpenEngine/OpenEngine/commit/2b4d8dc8b966083b077404d7de0fe4dd10762bad))
* add github comments graph migration ([#380](https://github.com/OpenEngine/OpenEngine/issues/380)) ([10b7a93](https://github.com/OpenEngine/OpenEngine/commit/10b7a931c940b792fd1c782c89c287aaf186bb9f)), closes [#368](https://github.com/OpenEngine/OpenEngine/issues/368)
* add GithubIngress webhook route ([#384](https://github.com/OpenEngine/OpenEngine/issues/384)) ([ff14390](https://github.com/OpenEngine/OpenEngine/commit/ff143901d928e5ca5570bee29d9424bdb62dcd14))
* add impact analysis to the default workflow ([#439](https://github.com/OpenEngine/OpenEngine/issues/439)) ([f83b04f](https://github.com/OpenEngine/OpenEngine/commit/f83b04f5160fe841c4bd9b88eb5908ac0f4a3a36))
* add interactive approval runner seam ([#36](https://github.com/OpenEngine/OpenEngine/issues/36)) ([70fdd34](https://github.com/OpenEngine/OpenEngine/commit/70fdd34f243fd7e70e9fbf4faa12b0f8c184a328))
* add least-utilized and round-robin runner choices ([#542](https://github.com/OpenEngine/OpenEngine/issues/542)) ([00b5183](https://github.com/OpenEngine/OpenEngine/commit/00b51839ccab39d6288e424c125cfd502a6335a8))
* add per-node runner selection to graph workflows ([#339](https://github.com/OpenEngine/OpenEngine/issues/339)) ([6d49d4a](https://github.com/OpenEngine/OpenEngine/commit/6d49d4a3a85cf8a8899d2d69c242d54ff1a75f87))
* add read-only repository tools to Slack concierge ([#465](https://github.com/OpenEngine/OpenEngine/issues/465)) ([c7be73b](https://github.com/OpenEngine/OpenEngine/commit/c7be73b82cd6657574f407985f1feeab449c8d72))
* add retry to conversation errors ([#349](https://github.com/OpenEngine/OpenEngine/issues/349)) ([cf2bd7c](https://github.com/OpenEngine/OpenEngine/commit/cf2bd7c801142cd7f4a304b5a7973acc3bb9ebb1))
* add Stop button to graph node conversations ([#350](https://github.com/OpenEngine/OpenEngine/issues/350)) ([9069636](https://github.com/OpenEngine/OpenEngine/commit/90696365cf5ea37cd72804d7d8bd3c489c65dbc0))
* add workorder accordion filters ([#346](https://github.com/OpenEngine/OpenEngine/issues/346)) ([1d4b865](https://github.com/OpenEngine/OpenEngine/commit/1d4b86572ffdc704c8ba3521ec3f342981b16725))
* **agent-runner:** add interactive approval runners ([#51](https://github.com/OpenEngine/OpenEngine/issues/51)) ([8992ae7](https://github.com/OpenEngine/OpenEngine/commit/8992ae7e57d80106afe336f43e7bf8af3b4a6093))
* **agent-runner:** require permission translators ([a825608](https://github.com/OpenEngine/OpenEngine/commit/a82560880911a2b45e095849b140c12cc1befdcc))
* allow detach/reattach of worktree ([4ea1584](https://github.com/OpenEngine/OpenEngine/commit/4ea158470de023d94768aa168ab4987d6f200390))
* **approval:** add approval UI, session grants, and provider compatibility tests ([#56](https://github.com/OpenEngine/OpenEngine/issues/56)) ([841bbc3](https://github.com/OpenEngine/OpenEngine/commit/841bbc3ee2e4371c0271610d02593c9e3a94a3c5))
* **approvals:** add human plan and question prompts ([#92](https://github.com/OpenEngine/OpenEngine/issues/92)) ([a4d8499](https://github.com/OpenEngine/OpenEngine/commit/a4d8499750540cb35142e408dbde66685f762254))
* **approvals:** enforce the configured approval policy ([#84](https://github.com/OpenEngine/OpenEngine/issues/84)) ([a0fa6ae](https://github.com/OpenEngine/OpenEngine/commit/a0fa6ae335565738a2611580fee52894969f420b))
* **auth:** add GitHub web OAuth identity verification ([#310](https://github.com/OpenEngine/OpenEngine/issues/310)) ([a888870](https://github.com/OpenEngine/OpenEngine/commit/a8888701e8acf35f03dda52ea8652ae372f51ac7))
* **auth:** add login page with GitHub OAuth session management ([#335](https://github.com/OpenEngine/OpenEngine/issues/335)) ([00485f1](https://github.com/OpenEngine/OpenEngine/commit/00485f17f87908bbc1781a7d6dbef2963c19a42f))
* auto-check auto-approve for graph workflows when config enables it ([#334](https://github.com/OpenEngine/OpenEngine/issues/334)) ([65d71ba](https://github.com/OpenEngine/OpenEngine/commit/65d71baac892c0794a471c432e8ee796374437a8))
* **cli:** add terminal workbench and local service management ([#528](https://github.com/OpenEngine/OpenEngine/issues/528)) ([81e9923](https://github.com/OpenEngine/OpenEngine/commit/81e9923d5e5f909dfd078b559057ee78f8f341b8))
* collapse conversation headers on mobile ([#483](https://github.com/OpenEngine/OpenEngine/issues/483)) ([604b052](https://github.com/OpenEngine/OpenEngine/commit/604b052e09acc8713a966b9684c7c88f686aba84))
* **communications:** start work orders by pinging the bot ([#277](https://github.com/OpenEngine/OpenEngine/issues/277)) ([8cd7e7e](https://github.com/OpenEngine/OpenEngine/commit/8cd7e7e0e1c0a8d7f9c78f1673679d61a4ac1f44))
* **config:** configure the GitHub webhook repository and secret ([#386](https://github.com/OpenEngine/OpenEngine/issues/386)) ([f848d24](https://github.com/OpenEngine/OpenEngine/commit/f848d2499042596277d180e740ffcf4fa50267e0))
* **config:** let engine.toml choose an agent's response style ([#135](https://github.com/OpenEngine/OpenEngine/issues/135)) ([2a9b56c](https://github.com/OpenEngine/OpenEngine/commit/2a9b56cfe30c3c9d582ebf15ae5351d0d393c5e6))
* **config:** load provider-neutral approval settings ([#58](https://github.com/OpenEngine/OpenEngine/issues/58)) ([5717e9b](https://github.com/OpenEngine/OpenEngine/commit/5717e9b2ef4f1206fe7c039167a375a92aa951db))
* **config:** move Slack notifications to engine config ([#261](https://github.com/OpenEngine/OpenEngine/issues/261)) ([edc3c26](https://github.com/OpenEngine/OpenEngine/commit/edc3c269a86a78b7e3a72c27d60d808c2b545dc6)), closes [#260](https://github.com/OpenEngine/OpenEngine/issues/260)
* **config:** support disabling agent attribution ([#80](https://github.com/OpenEngine/OpenEngine/issues/80)) ([ec16d3b](https://github.com/OpenEngine/OpenEngine/commit/ec16d3b13a61d340124df0d6143dd74c082b9241))
* configure projects accordion visibility in TOML ([#451](https://github.com/OpenEngine/OpenEngine/issues/451)) ([24593ab](https://github.com/OpenEngine/OpenEngine/commit/24593abec98ad29cff0877ae3d9815c612b3d728))
* control WorkOrders through linked Slack threads ([#452](https://github.com/OpenEngine/OpenEngine/issues/452)) ([5e22550](https://github.com/OpenEngine/OpenEngine/commit/5e22550c45c70fbd5835812946425deebdd052ed))
* create tasks from milestone pages ([#161](https://github.com/OpenEngine/OpenEngine/issues/161)) ([ac467d9](https://github.com/OpenEngine/OpenEngine/commit/ac467d99366ef44e02137ff3bfba09c43e39e1a1))
* dev server ([#192](https://github.com/OpenEngine/OpenEngine/issues/192)) ([a965305](https://github.com/OpenEngine/OpenEngine/commit/a965305429af9326e167ab243d4e9e5bfddf35d9))
* display graph node outputs and PR links in workorder overview ([#338](https://github.com/OpenEngine/OpenEngine/issues/338)) ([d7f069c](https://github.com/OpenEngine/OpenEngine/commit/d7f069c0b4e0d732a352123026444c00929d36b9))
* **domain:** add workflow step vocabulary ([#35](https://github.com/OpenEngine/OpenEngine/issues/35)) ([a3212be](https://github.com/OpenEngine/OpenEngine/commit/a3212be977935bad78fd54ba0511ab5a160dcaef))
* durable approvals and broker api ([#55](https://github.com/OpenEngine/OpenEngine/issues/55)) ([2f481e1](https://github.com/OpenEngine/OpenEngine/commit/2f481e1de57c1594439e578b2512ade4ec05e89c))
* **engine:** add implementation review workflow ([#40](https://github.com/OpenEngine/OpenEngine/issues/40)) ([43c6f11](https://github.com/OpenEngine/OpenEngine/commit/43c6f1103519e67180b70bf6658280e60504304c))
* **engine:** enable auto approval ([ee9a51e](https://github.com/OpenEngine/OpenEngine/commit/ee9a51e4ce65e448a0eae8acfad4dc424dbf5ad1))
* expose immediate work orders through remote MCP ([#455](https://github.com/OpenEngine/OpenEngine/issues/455)) ([55191f3](https://github.com/OpenEngine/OpenEngine/commit/55191f34bf8e53e8da5198cb32a817d5e52852ad))
* fan out review stage to four faceted reviewers with reranker ([#337](https://github.com/OpenEngine/OpenEngine/issues/337)) ([393db53](https://github.com/OpenEngine/OpenEngine/commit/393db53153d671b4d6ae8e29db851418aaa5ceb2))
* gate graph transitions on terminal results ([#318](https://github.com/OpenEngine/OpenEngine/issues/318)) ([19ffd53](https://github.com/OpenEngine/OpenEngine/commit/19ffd534710500944faa591d1a4e09836234ed02))
* gate implementation review on deterministic CI checks ([#403](https://github.com/OpenEngine/OpenEngine/issues/403)) ([3680895](https://github.com/OpenEngine/OpenEngine/commit/36808955df9d7b9fb8af4412f5f24074ee4bd024))
* github oauth device flow ([#170](https://github.com/OpenEngine/OpenEngine/issues/170)) ([28c3189](https://github.com/OpenEngine/OpenEngine/commit/28c3189075b3d4278132c123002ac874db574c63))
* **github:** let merging a pull request approve its work order ([#420](https://github.com/OpenEngine/OpenEngine/issues/420)) ([825f05f](https://github.com/OpenEngine/OpenEngine/commit/825f05fbc6c051e8a9982cfe70d077f10c242aa8))
* **github:** route a pull-request comment to a steer or a new work order ([#400](https://github.com/OpenEngine/OpenEngine/issues/400)) ([345e5f7](https://github.com/OpenEngine/OpenEngine/commit/345e5f7a7a267be4b92c928aa386b60a2c9ab970))
* **gitlab:** add OAuth device flow foundation ([#258](https://github.com/OpenEngine/OpenEngine/issues/258)) ([9ab2594](https://github.com/OpenEngine/OpenEngine/commit/9ab25947da2492237e6d9c827b8eca348d1a24f5))
* **graph-runtime:** add reusable graph components and a workflow API ([#236](https://github.com/OpenEngine/OpenEngine/issues/236)) ([3d8869d](https://github.com/OpenEngine/OpenEngine/commit/3d8869d1cca766d84e089b5b7e0d5a690ca122aa))
* **graph-runtime:** add the graph control surface ([#216](https://github.com/OpenEngine/OpenEngine/issues/216)) ([5bb7415](https://github.com/OpenEngine/OpenEngine/commit/5bb74159f094f5a46495a92ef57fe5dc10cddee9))
* **graph-runtime:** implement the contract against LangGraph ([#232](https://github.com/OpenEngine/OpenEngine/issues/232)) ([7b132a0](https://github.com/OpenEngine/OpenEngine/commit/7b132a0d2c4a654c35ce94032fea7a219cb7afe6))
* **graph:** add always_open node setting for re-entrant steering ([#320](https://github.com/OpenEngine/OpenEngine/issues/320)) ([ce1335c](https://github.com/OpenEngine/OpenEngine/commit/ce1335c2ec0079195bc82e73ad81247ff4ba4ced))
* **graph:** give the review node the same tools as the classic reviewer ([#321](https://github.com/OpenEngine/OpenEngine/issues/321)) ([8f6aefe](https://github.com/OpenEngine/OpenEngine/commit/8f6aefe7539510e3779e335664c844ae96ff6970))
* **graph:** offer a graph WorkOrder's conversations in the rail ([#279](https://github.com/OpenEngine/OpenEngine/issues/279)) ([eba6380](https://github.com/OpenEngine/OpenEngine/commit/eba638033d7b339d152695e48eb2e7244527ecc5))
* **graph:** read and steer a graph node's conversation in the chat view ([#278](https://github.com/OpenEngine/OpenEngine/issues/278)) ([63e15f8](https://github.com/OpenEngine/OpenEngine/commit/63e15f83ed90daa5dcf5949b6bbd5ee65dac69f0))
* group the reranker under Review ([#437](https://github.com/OpenEngine/OpenEngine/issues/437)) ([939bbe1](https://github.com/OpenEngine/OpenEngine/commit/939bbe11035323f28c418dabd447f95c87ce6d13))
* **langgraph-acp:** add ACP provider registry ([#155](https://github.com/OpenEngine/OpenEngine/issues/155)) ([12810f9](https://github.com/OpenEngine/OpenEngine/commit/12810f9709974b1bcd6e328912cdb62910053568))
* **langgraph-acp:** add minimal ACP node ([#165](https://github.com/OpenEngine/OpenEngine/issues/165)) ([e797833](https://github.com/OpenEngine/OpenEngine/commit/e797833b59a670d3e8bb6853371472093d4bc4c4))
* **langgraph-acp:** bind LangGraph identity to ACP sessions ([#158](https://github.com/OpenEngine/OpenEngine/issues/158)) ([48edca0](https://github.com/OpenEngine/OpenEngine/commit/48edca0935e4e6d1d7da7d7c85bd7ec8dbc2c011))
* **langgraph-acp:** scaffold the package and its core types ([#133](https://github.com/OpenEngine/OpenEngine/issues/133)) ([eeecf30](https://github.com/OpenEngine/OpenEngine/commit/eeecf303753a44f7ab72feef3e16f33d475c2cd8))
* let implementers and rerankers create workorders with provenance ([#438](https://github.com/OpenEngine/OpenEngine/issues/438)) ([693577c](https://github.com/OpenEngine/OpenEngine/commit/693577ce1186d339c539c379303a2f5e54ffb0fb))
* **mcp:** add OIDC resource server with email allowlist ([#468](https://github.com/OpenEngine/OpenEngine/issues/468)) ([1ff9d0b](https://github.com/OpenEngine/OpenEngine/commit/1ff9d0b9eaf2438ec9380e806bdbc026c3d75b58))
* **mcp:** expose work order dependency parameter ([#466](https://github.com/OpenEngine/OpenEngine/issues/466)) ([9a739d6](https://github.com/OpenEngine/OpenEngine/commit/9a739d6e6071e90a99d762a8e49cfa520959852e))
* mirror agent transcript text to originating Slack threads ([#529](https://github.com/OpenEngine/OpenEngine/issues/529)) ([9a38c4b](https://github.com/OpenEngine/OpenEngine/commit/9a38c4b50d707e706051a5ddb91368e23d717cc6))
* named conversations ([#13](https://github.com/OpenEngine/OpenEngine/issues/13)) ([1fd0886](https://github.com/OpenEngine/OpenEngine/commit/1fd0886aac21f69209628209cc79b7cd84498522))
* notify Slack when human review is needed ([#255](https://github.com/OpenEngine/OpenEngine/issues/255)) ([df85b3d](https://github.com/OpenEngine/OpenEngine/commit/df85b3d188e24d346db60097611b77acba3be412))
* **orchestrator:** run supervised local Temporal service ([#249](https://github.com/OpenEngine/OpenEngine/issues/249)) ([ffc2261](https://github.com/OpenEngine/OpenEngine/commit/ffc22610fcca23ea426fce310b18cd7355833411))
* per-conversation workspaces ([#16](https://github.com/OpenEngine/OpenEngine/issues/16)) ([45ff0a5](https://github.com/OpenEngine/OpenEngine/commit/45ff0a5154bdc111f628e91bc64834ed39fa1288))
* **planning:** add planner milestone tools ([#122](https://github.com/OpenEngine/OpenEngine/issues/122)) ([d5bd169](https://github.com/OpenEngine/OpenEngine/commit/d5bd169c1df917cc75cdbfe77da6517ebe4575b5))
* **planning:** add project work hierarchy ([#112](https://github.com/OpenEngine/OpenEngine/issues/112)) ([3bfc17a](https://github.com/OpenEngine/OpenEngine/commit/3bfc17a7cfa8072d209aad58c2bfea5d8e46ac5c))
* **planning:** let the planner record workstreams ([#139](https://github.com/OpenEngine/OpenEngine/issues/139)) ([2e1aeba](https://github.com/OpenEngine/OpenEngine/commit/2e1aeba2994aa9588f3bcdd2a7b2f4b0b8cedca4))
* post impact analysis results as a PR comment ([#485](https://github.com/OpenEngine/OpenEngine/issues/485)) ([b9a5bd8](https://github.com/OpenEngine/OpenEngine/commit/b9a5bd851331baabb1b34847d9878b1cb085a1ae))
* provide workflow tools to graph agents ([#312](https://github.com/OpenEngine/OpenEngine/issues/312)) ([b3cb665](https://github.com/OpenEngine/OpenEngine/commit/b3cb665bf3d189b37d43f4b3b3ec87857694e866))
* record posted pull-request comments in the graph store ([#385](https://github.com/OpenEngine/OpenEngine/issues/385)) ([61ceae0](https://github.com/OpenEngine/OpenEngine/commit/61ceae0d676f2c4876ba140a926a49d16a4a6e5d))
* record who started a work order ([#516](https://github.com/OpenEngine/OpenEngine/issues/516)) ([11fb4a9](https://github.com/OpenEngine/OpenEngine/commit/11fb4a9fec99ab4fbf73aba7bee6575a80cb0372))
* **release:** automate releases and bundle application wheels ([#501](https://github.com/OpenEngine/OpenEngine/issues/501)) ([3b490a3](https://github.com/OpenEngine/OpenEngine/commit/3b490a319ff592cbcf9a8fbab7e6daec741c81e0))
* require repository write access to sign in ([#530](https://github.com/OpenEngine/OpenEngine/issues/530)) ([e0fbf17](https://github.com/OpenEngine/OpenEngine/commit/e0fbf17e94300ff9e6455c2de091ca889b3216b5))
* restore auto-approve for graph workflow conversations ([#317](https://github.com/OpenEngine/OpenEngine/issues/317)) ([d10cdf8](https://github.com/OpenEngine/OpenEngine/commit/d10cdf8958d0ff844a654720bf13a331f24d0284))
* retire the step workflow and the graph's BETA label ([#387](https://github.com/OpenEngine/OpenEngine/issues/387)) ([cd8aa70](https://github.com/OpenEngine/OpenEngine/commit/cd8aa70c5960396cd94c65415c6f1c84d5d3b9ba))
* return comment provenance and support review thread replies ([#381](https://github.com/OpenEngine/OpenEngine/issues/381)) ([f9f42bc](https://github.com/OpenEngine/OpenEngine/commit/f9f42bc7fdf5687aaff226853afe0bca1b553c22))
* **review:** post findings to pull requests ([#69](https://github.com/OpenEngine/OpenEngine/issues/69)) ([dc5bc7f](https://github.com/OpenEngine/OpenEngine/commit/dc5bc7f73045286dc4a813cc073d57b648e68273))
* route review feedback to implementation for one cycle ([#434](https://github.com/OpenEngine/OpenEngine/issues/434)) ([d8b02ca](https://github.com/OpenEngine/OpenEngine/commit/d8b02ca72507c42ac5413c31c06ffa207677ccf6))
* run web chat and composed agent runners over ACP ([#522](https://github.com/OpenEngine/OpenEngine/issues/522)) ([1756786](https://github.com/OpenEngine/OpenEngine/commit/1756786d9e4fc743ec23fbc92fff047919256a98))
* **runtime:** name a run with the repository tools ([#273](https://github.com/OpenEngine/OpenEngine/issues/273)) ([3071499](https://github.com/OpenEngine/OpenEngine/commit/30714997aaa83007e101158407df20f583dae87c))
* **runtime:** parse workflow step results ([#39](https://github.com/OpenEngine/OpenEngine/issues/39)) ([bc16ba7](https://github.com/OpenEngine/OpenEngine/commit/bc16ba7c2f07ca54d0d620d22937693331dfb768))
* scaffold first-class workflow orchestration ([#242](https://github.com/OpenEngine/OpenEngine/issues/242)) ([d8708ae](https://github.com/OpenEngine/OpenEngine/commit/d8708aece9a716521a97ad42ea6457f8d6b2aa00))
* schedule scoped workorders until explicitly started ([#390](https://github.com/OpenEngine/OpenEngine/issues/390)) ([e007b5a](https://github.com/OpenEngine/OpenEngine/commit/e007b5a2fd8112b06245274b2606f9ff807030a8))
* schedule work orders behind prerequisite dependencies ([#454](https://github.com/OpenEngine/OpenEngine/issues/454)) ([73e6e70](https://github.com/OpenEngine/OpenEngine/commit/73e6e7060051692a5305bc9669fb05373e802784))
* **scoper:** define work order scoping contract ([#208](https://github.com/OpenEngine/OpenEngine/issues/208)) ([876896e](https://github.com/OpenEngine/OpenEngine/commit/876896e9de2fa6157fd62ab2a3a59517beb2d33f))
* **scoper:** implement ACP-backed scoping ([#211](https://github.com/OpenEngine/OpenEngine/issues/211)) ([12d6081](https://github.com/OpenEngine/OpenEngine/commit/12d608153e5b2e7875a92aa09fc98dd8c9992d46))
* **site:** add a landing page deployed to GitHub Pages ([#32](https://github.com/OpenEngine/OpenEngine/issues/32)) ([d8a1c57](https://github.com/OpenEngine/OpenEngine/commit/d8a1c575559366917b301ecb639112b3843518b2))
* **site:** rebuild the landing page on the product's theme ([#262](https://github.com/OpenEngine/OpenEngine/issues/262)) ([ab2240a](https://github.com/OpenEngine/OpenEngine/commit/ab2240ae09c2cbd464878418ea261e4cefadeb4b))
* **slack:** add a LangGraph ACP concierge with separate ingress and egress ([#336](https://github.com/OpenEngine/OpenEngine/issues/336)) ([bb2f771](https://github.com/OpenEngine/OpenEngine/commit/bb2f77187e036b3afa8ca6dca95fb4eb735ca647))
* **slack:** enable interactive work order control ([#397](https://github.com/OpenEngine/OpenEngine/issues/397)) ([ebc23c6](https://github.com/OpenEngine/OpenEngine/commit/ebc23c66767cf2f13020dbc483ad320770307cb5))
* **slack:** react with eyes emoji on incoming messages ([#356](https://github.com/OpenEngine/OpenEngine/issues/356)) ([e4a2769](https://github.com/OpenEngine/OpenEngine/commit/e4a2769c2141ef93477ceec5cb6ade53e83548ad))
* **source-control:** add forge-neutral agent inspection tools ([#212](https://github.com/OpenEngine/OpenEngine/issues/212)) ([eab826c](https://github.com/OpenEngine/OpenEngine/commit/eab826c0a7e1a7e8e96a424247322e126cc14520))
* **source-control:** give agents git and pull requests as tools ([#150](https://github.com/OpenEngine/OpenEngine/issues/150)) ([be94694](https://github.com/OpenEngine/OpenEngine/commit/be946945ad2d68ed11aa919554e5345910f40446))
* **source-control:** restore gh cli provider selection ([#233](https://github.com/OpenEngine/OpenEngine/issues/233)) ([99e3c4a](https://github.com/OpenEngine/OpenEngine/commit/99e3c4afe881c09b03cf76973e8a9000b98d0956))
* start work orders from GitHub issue assignments ([#481](https://github.com/OpenEngine/OpenEngine/issues/481)) ([dacd0d5](https://github.com/OpenEngine/OpenEngine/commit/dacd0d5da111f6da6ca3a7e974b56c981f8a60e6)), closes [#414](https://github.com/OpenEngine/OpenEngine/issues/414)
* **storage:** migrate schemas to alembic ([#113](https://github.com/OpenEngine/OpenEngine/issues/113)) ([9514748](https://github.com/OpenEngine/OpenEngine/commit/9514748f157b62fa5d0ea2197b3ddf56d975a17a))
* stream messages to the frontend ([#9](https://github.com/OpenEngine/OpenEngine/issues/9)) ([0e1a547](https://github.com/OpenEngine/OpenEngine/commit/0e1a547b9747a0999bab856a9602b5044d107912))
* support graph workflow inputs and independent stage runners ([#367](https://github.com/OpenEngine/OpenEngine/issues/367)) ([a01801f](https://github.com/OpenEngine/OpenEngine/commit/a01801f902d3e8077dfc508cb81978865687c96d))
* **web:** add detach to workflow checkouts ([#93](https://github.com/OpenEngine/OpenEngine/issues/93)) ([2180a27](https://github.com/OpenEngine/OpenEngine/commit/2180a2713279ae9ab5573fc9bd01d9088c15cf8e))
* **web:** add executable workflow creation ([#46](https://github.com/OpenEngine/OpenEngine/issues/46)) ([582b451](https://github.com/OpenEngine/OpenEngine/commit/582b451b22a54fca4643d8eb14deb11a5bd3ee60))
* **web:** add implementation auto-approve toggle ([#89](https://github.com/OpenEngine/OpenEngine/issues/89)) ([8aa4641](https://github.com/OpenEngine/OpenEngine/commit/8aa464105f9e1a5534fbbfd998989efe352d0e2b))
* **web:** add milestone scope action ([#295](https://github.com/OpenEngine/OpenEngine/issues/295)) ([cb8ac8c](https://github.com/OpenEngine/OpenEngine/commit/cb8ac8c712d1d3a9a685f9e18e88b713e0889899))
* **web:** add new project creation flow ([#123](https://github.com/OpenEngine/OpenEngine/issues/123)) ([878a5fc](https://github.com/OpenEngine/OpenEngine/commit/878a5fcb85cc23e189814dad658d951ddca64912))
* **web:** add Slack OAuth settings ([#252](https://github.com/OpenEngine/OpenEngine/issues/252)) ([6991df4](https://github.com/OpenEngine/OpenEngine/commit/6991df416efa50ccc5089c4f7668c1acb0ab48d1))
* **web:** archive projects from the rail ([#137](https://github.com/OpenEngine/OpenEngine/issues/137)) ([870c779](https://github.com/OpenEngine/OpenEngine/commit/870c779134e18a36022e29bad39b503805153ec7))
* **web:** archive WorkOrders that passed human review ([#450](https://github.com/OpenEngine/OpenEngine/issues/450)) ([16ae702](https://github.com/OpenEngine/OpenEngine/commit/16ae702e008342c0522fe2a76c580902ea7e2fcc))
* **web:** auto-approve review conversations ([#101](https://github.com/OpenEngine/OpenEngine/issues/101)) ([62645b0](https://github.com/OpenEngine/OpenEngine/commit/62645b05deb1c5c8bebcba197665f7efab14d5cd))
* **web:** decide a workflow run's human review from the run page ([#107](https://github.com/OpenEngine/OpenEngine/issues/107)) ([dc7366f](https://github.com/OpenEngine/OpenEngine/commit/dc7366f86a05c12266cdd7f7641f6914c943f7bf))
* **web:** delete a WorkOrder from the rail ([#267](https://github.com/OpenEngine/OpenEngine/issues/267)) ([da73dcd](https://github.com/OpenEngine/OpenEngine/commit/da73dcd3a006f2c0b8f606a92325abba3727ef49))
* **web:** filter milestone tasks by workstream ([#167](https://github.com/OpenEngine/OpenEngine/issues/167)) ([151e3a8](https://github.com/OpenEngine/OpenEngine/commit/151e3a814ef7bc21b0bf8a5cfba441fd1332e8eb))
* **web:** filter WorkOrders by title ([#417](https://github.com/OpenEngine/OpenEngine/issues/417)) ([66963c6](https://github.com/OpenEngine/OpenEngine/commit/66963c681fd6064ce6d26ce04083a9433283dde6))
* **web:** give a planned project a milestones page ([#142](https://github.com/OpenEngine/OpenEngine/issues/142)) ([7fd8920](https://github.com/OpenEngine/OpenEngine/commit/7fd8920af86a5b99ffc8932896ff02c59fd1551c))
* **web:** give the milestones page scheduled and finished work tables ([#401](https://github.com/OpenEngine/OpenEngine/issues/401)) ([230b644](https://github.com/OpenEngine/OpenEngine/commit/230b644196126251aee4f260d7556e10033926ad))
* **web:** keep the milestone timeline current ([#131](https://github.com/OpenEngine/OpenEngine/issues/131)) ([f2a8087](https://github.com/OpenEngine/OpenEngine/commit/f2a8087ce61ee6b2151386c09fdc179ca8db90dd))
* **web:** link to the pull request awaiting human review ([#210](https://github.com/OpenEngine/OpenEngine/issues/210)) ([c0c4de7](https://github.com/OpenEngine/OpenEngine/commit/c0c4de76e86af388993add75e8fc077374493bfd))
* **web:** list a milestone's workstreams on the timeline ([#141](https://github.com/OpenEngine/OpenEngine/issues/141)) ([dbc96f4](https://github.com/OpenEngine/OpenEngine/commit/dbc96f4e3fe8917640a1cac20a63ef0243c54ffb))
* **web:** make workflow runs the primary work view ([#44](https://github.com/OpenEngine/OpenEngine/issues/44)) ([1f15951](https://github.com/OpenEngine/OpenEngine/commit/1f15951e6b7804f5e4d1487c505f90e826754b27))
* **web:** mark waiting workflow conversations ([#91](https://github.com/OpenEngine/OpenEngine/issues/91)) ([7ffa285](https://github.com/OpenEngine/OpenEngine/commit/7ffa285081c8fe39a35ea6d4434ff30d5ff256f3))
* **web:** offer graph workflows as [BETA] WorkOrders ([#251](https://github.com/OpenEngine/OpenEngine/issues/251)) ([3d33d2a](https://github.com/OpenEngine/OpenEngine/commit/3d33d2a204ecd4a23268159621885c1feb5da912))
* **web:** open a milestone's page from a workstream on the timeline ([#152](https://github.com/OpenEngine/OpenEngine/issues/152)) ([affac8b](https://github.com/OpenEngine/OpenEngine/commit/affac8b818fca646c86cdaf312c5132ccda667f7))
* **web:** open milestone details from cards ([#156](https://github.com/OpenEngine/OpenEngine/issues/156)) ([b5aa258](https://github.com/OpenEngine/OpenEngine/commit/b5aa258d30488d20b0cdeee351ae825dc746db95))
* **web:** queue messages during active runs ([#37](https://github.com/OpenEngine/OpenEngine/issues/37)) ([08cad35](https://github.com/OpenEngine/OpenEngine/commit/08cad35a741d29f53d2c16609bebd9d93db45056))
* **web:** read runner utilization from the rail ([#291](https://github.com/OpenEngine/OpenEngine/issues/291)) ([edd01c6](https://github.com/OpenEngine/OpenEngine/commit/edd01c65932f80e47e64b935756859aecd5d6131))
* **web:** rebuild the UI on the OpenEngine design system ([#63](https://github.com/OpenEngine/OpenEngine/issues/63)) ([e6ce60f](https://github.com/OpenEngine/OpenEngine/commit/e6ce60f2ba796ad20e28bc30fbdc9a3d57e6c6c1))
* **web:** refine new project conversations ([#132](https://github.com/OpenEngine/OpenEngine/issues/132)) ([0c4c8b7](https://github.com/OpenEngine/OpenEngine/commit/0c4c8b7fa60aa639efadc094af7fb429e03fa96c))
* **web:** remember the last runner selection on the new WorkOrder form ([#546](https://github.com/OpenEngine/OpenEngine/issues/546)) ([f76d81c](https://github.com/OpenEngine/OpenEngine/commit/f76d81cea277bf23bd8e5f6c95e1e363016487de))
* **web:** rename workflow runs to WorkOrders ([#205](https://github.com/OpenEngine/OpenEngine/issues/205)) ([c63e074](https://github.com/OpenEngine/OpenEngine/commit/c63e074970c770f8732687873fdcb0f5f6fc779f))
* **web:** reorganize the sidebar into three sections ([#85](https://github.com/OpenEngine/OpenEngine/issues/85)) ([aa76860](https://github.com/OpenEngine/OpenEngine/commit/aa76860bcb57e12ca4118d799e182d2fadba4dba))
* **web:** retain the new-workflow draft prompt ([#78](https://github.com/OpenEngine/OpenEngine/issues/78)) ([a98fe78](https://github.com/OpenEngine/OpenEngine/commit/a98fe788e48ebac87a20536416a7f3cee119d790))
* **web:** select the runner per conversation ([#52](https://github.com/OpenEngine/OpenEngine/issues/52)) ([72f787a](https://github.com/OpenEngine/OpenEngine/commit/72f787a5afce8c08674ab60409a8df886fc3b1ba))
* **web:** show a reopened run as in progress again ([#159](https://github.com/OpenEngine/OpenEngine/issues/159)) ([1226336](https://github.com/OpenEngine/OpenEngine/commit/1226336c5001a1175710e8acc72aefa54dd771ec))
* **web:** show live workflow status indicator ([#73](https://github.com/OpenEngine/OpenEngine/issues/73)) ([691b437](https://github.com/OpenEngine/OpenEngine/commit/691b4375047b31f82cf68d23032f6549c959a1e1))
* **web:** show what GitHub comments asked of the engine ([#407](https://github.com/OpenEngine/OpenEngine/issues/407)) ([4e13835](https://github.com/OpenEngine/OpenEngine/commit/4e138353a8887c51124ae7789fe0d2f9d65295ff))
* **web:** start a planning conversation from the projects rail ([#110](https://github.com/OpenEngine/OpenEngine/issues/110)) ([2b950a8](https://github.com/OpenEngine/OpenEngine/commit/2b950a873fdca9882fa99c1d8bb1fff9fb4cc78f))
* **web:** stream implementation conversation progress ([#62](https://github.com/OpenEngine/OpenEngine/issues/62)) ([1eca587](https://github.com/OpenEngine/OpenEngine/commit/1eca58706fda6846561e01a60cd8df8f501b5b38))
* **web:** visualize project milestones ([#129](https://github.com/OpenEngine/OpenEngine/issues/129)) ([80c426a](https://github.com/OpenEngine/OpenEngine/commit/80c426a441f7c717a38122279e656cb620e05273))
* wire GitHub ingress worker to concierge ([#391](https://github.com/OpenEngine/OpenEngine/issues/391)) ([a55b7e6](https://github.com/OpenEngine/OpenEngine/commit/a55b7e60798ce01551dbfa326a32540ecfa1f2b0))
* **workflow:** add conversation approval handling ([#67](https://github.com/OpenEngine/OpenEngine/issues/67)) ([9877488](https://github.com/OpenEngine/OpenEngine/commit/9877488e8669f40f83f321db150c45666271195b))
* **workflow:** add non-mutating clarify tool ([#103](https://github.com/OpenEngine/OpenEngine/issues/103)) ([a35c77a](https://github.com/OpenEngine/OpenEngine/commit/a35c77a93feb0f8bb399829e32259cd7d3a4cb56))
* **workflow:** define terminal step tools ([#49](https://github.com/OpenEngine/OpenEngine/issues/49)) ([627f52f](https://github.com/OpenEngine/OpenEngine/commit/627f52f751765cc7e957b8944cdf73402dc886ae))
* **workflow:** execute reviewer step ([0ee1748](https://github.com/OpenEngine/OpenEngine/commit/0ee17485a200965df96f5e0b770f87736cf31012))
* **workflow:** execute terminal step tools through MCP ([#50](https://github.com/OpenEngine/OpenEngine/issues/50)) ([94a4404](https://github.com/OpenEngine/OpenEngine/commit/94a440426001421357a3341bbe00a5731ef3c438))
* **workflow:** load repository-defined workflows ([#76](https://github.com/OpenEngine/OpenEngine/issues/76)) ([482087d](https://github.com/OpenEngine/OpenEngine/commit/482087da455a6c0bb576e2a48e77a6bcc5a70514))
* **workflow:** make implementation conversations editable ([#70](https://github.com/OpenEngine/OpenEngine/issues/70)) ([8c0bb8d](https://github.com/OpenEngine/OpenEngine/commit/8c0bb8d3456b2f4fa2296540f4210a695badc564))
* **workflow:** name submitted runs with implementation runner ([#74](https://github.com/OpenEngine/OpenEngine/issues/74)) ([d15456f](https://github.com/OpenEngine/OpenEngine/commit/d15456fb7c2ee7c83298ac3c193b56beaa503013))
* **workflow:** require implementation PR URL ([#65](https://github.com/OpenEngine/OpenEngine/issues/65)) ([4136a33](https://github.com/OpenEngine/OpenEngine/commit/4136a33ab367ca8f233055d91660928ac5a12b42))
* **workflows:** add the implementation-review graph definition ([#240](https://github.com/OpenEngine/OpenEngine/issues/240)) ([369a48d](https://github.com/OpenEngine/OpenEngine/commit/369a48db8d93f12aaa5a43299d52e3cba1802fb6))
* **workflows:** allow conversation runner swaps ([#153](https://github.com/OpenEngine/OpenEngine/issues/153)) ([ba9ddda](https://github.com/OpenEngine/OpenEngine/commit/ba9dddaff2201be37655554359f6b64bd48bb489))
* **workflows:** name graph workorders ([#309](https://github.com/OpenEngine/OpenEngine/issues/309)) ([018c7bd](https://github.com/OpenEngine/OpenEngine/commit/018c7bd9b6b140390d77c128e94c9748e18e36c8))


### Bug Fixes

* **acp:** launch Claude through the renamed claude-agent-acp adapter ([#379](https://github.com/OpenEngine/OpenEngine/issues/379)) ([02ce54a](https://github.com/OpenEngine/OpenEngine/commit/02ce54afbcc78dc640090a12fabbbd6ce06ce384))
* **acp:** re-apply Claude's appended system prompt on session/load ([#544](https://github.com/OpenEngine/OpenEngine/issues/544)) ([8ca66d2](https://github.com/OpenEngine/OpenEngine/commit/8ca66d261402e71c2367ea2c123b6335bd8fc2fb))
* **acp:** say what an agent refused a turn for ([#266](https://github.com/OpenEngine/OpenEngine/issues/266)) ([33361ca](https://github.com/OpenEngine/OpenEngine/commit/33361ca4b075b387fbe62c6d16411e279aa4b7be))
* answer the message that reopens a clarified step ([#406](https://github.com/OpenEngine/OpenEngine/issues/406)) ([2e077bb](https://github.com/OpenEngine/OpenEngine/commit/2e077bb9e6cf39501a20cfe41b87c5e227bdaa56))
* apply Claude conciseness and attribution settings to output ([#340](https://github.com/OpenEngine/OpenEngine/issues/340)) ([613c05e](https://github.com/OpenEngine/OpenEngine/commit/613c05e6c6fda63f7f68af3775c94ad88434ff68))
* **approvals:** push updates to open conversations ([#90](https://github.com/OpenEngine/OpenEngine/issues/90)) ([abc0f4b](https://github.com/OpenEngine/OpenEngine/commit/abc0f4b447c4c37b6078a7d6d63db4b3c66e70af))
* **approvals:** show a repository-tool request beside its call ([#166](https://github.com/OpenEngine/OpenEngine/issues/166)) ([421d89d](https://github.com/OpenEngine/OpenEngine/commit/421d89de2872f978c88cea33d67ea3e0b43f86e2))
* **approvals:** show each request beside the call it was about ([#88](https://github.com/OpenEngine/OpenEngine/issues/88)) ([c7e2d22](https://github.com/OpenEngine/OpenEngine/commit/c7e2d2225bca61a5e5721cadae63c4bec16920bf))
* bound complete conversation replay prompts ([#344](https://github.com/OpenEngine/OpenEngine/issues/344)) ([86e27d2](https://github.com/OpenEngine/OpenEngine/commit/86e27d2e1c60021373c6951177ae49cf342d03be))
* clarify chat archive actions ([#29](https://github.com/OpenEngine/OpenEngine/issues/29)) ([9e2c6f3](https://github.com/OpenEngine/OpenEngine/commit/9e2c6f3bce4db718f6f4600e9f299a4a40387590))
* clarify Slack work order started confirmation ([#504](https://github.com/OpenEngine/OpenEngine/issues/504)) ([5786843](https://github.com/OpenEngine/OpenEngine/commit/57868437a4e082f8052656b9b830a852b3fdafec))
* **codex:** add protocol diagnostics ([#199](https://github.com/OpenEngine/OpenEngine/issues/199)) ([b4e3463](https://github.com/OpenEngine/OpenEngine/commit/b4e34634fa67ea737cba287ee121e764a1b5e6da))
* **codex:** elicitation and git commends cause execution errors ([#191](https://github.com/OpenEngine/OpenEngine/issues/191)) ([6125a2b](https://github.com/OpenEngine/OpenEngine/commit/6125a2b7824c7847986cab3537a01e5327180fd1))
* **codex:** recognize workflow clarification calls ([#115](https://github.com/OpenEngine/OpenEngine/issues/115)) ([392256f](https://github.com/OpenEngine/OpenEngine/commit/392256faa543e2f1b79384426e3e6cb9dd609d6f))
* **codex:** restore project milestone tools ([#209](https://github.com/OpenEngine/OpenEngine/issues/209)) ([da7be64](https://github.com/OpenEngine/OpenEngine/commit/da7be64dac63f78c55aa74a0f9b9d945bc5cb129))
* **codex:** support empty MCP approval forms ([#201](https://github.com/OpenEngine/OpenEngine/issues/201)) ([568fd47](https://github.com/OpenEngine/OpenEngine/commit/568fd4740e0cbe8be2f54efcbcbdc88da9101adf))
* configure workflow default branch (Fixes [#144](https://github.com/OpenEngine/OpenEngine/issues/144)) ([#162](https://github.com/OpenEngine/OpenEngine/issues/162)) ([e5a6737](https://github.com/OpenEngine/OpenEngine/commit/e5a673778d7c3f72e6c5d98e5b2d8c8734d49811))
* conversation titles updating only after refresh ([#22](https://github.com/OpenEngine/OpenEngine/issues/22)) ([077cc9e](https://github.com/OpenEngine/OpenEngine/commit/077cc9e32c2f5b308f028b0fe33a2e154ce2c636))
* **deps:** upgrade uuid to 11.1.1 ([#536](https://github.com/OpenEngine/OpenEngine/issues/536)) ([b95d569](https://github.com/OpenEngine/OpenEngine/commit/b95d56918d34b187e50b09ebe41d6d8710395eab))
* drain ACP steering before honoring terminal results ([#395](https://github.com/OpenEngine/OpenEngine/issues/395)) ([74d2b4c](https://github.com/OpenEngine/OpenEngine/commit/74d2b4cf8cdb2b4185597d6cb4e9e3bc2e36ad84)), closes [#374](https://github.com/OpenEngine/OpenEngine/issues/374)
* drop the graph approval's decision note box ([#389](https://github.com/OpenEngine/OpenEngine/issues/389)) ([3e97a08](https://github.com/OpenEngine/OpenEngine/commit/3e97a0897959467867517ea8f2929e6a7a5f5f53))
* durable sessions ([#15](https://github.com/OpenEngine/OpenEngine/issues/15)) ([ca35f36](https://github.com/OpenEngine/OpenEngine/commit/ca35f367d3e90276f30f241e2c65413d4bedb024))
* **e2e:** call add_comment before complete_step in graph review scenarios ([#329](https://github.com/OpenEngine/OpenEngine/issues/329)) ([64f5e2d](https://github.com/OpenEngine/OpenEngine/commit/64f5e2d32584f6e432e8a6dd217c7cb772ffa2f7))
* edit Slack progress notifications with timestamped history ([#503](https://github.com/OpenEngine/OpenEngine/issues/503)) ([5bde55f](https://github.com/OpenEngine/OpenEngine/commit/5bde55f4b248439cb2478973e64105cb43d34dbb))
* expose active beta conversations ([#272](https://github.com/OpenEngine/OpenEngine/issues/272)) ([e42344f](https://github.com/OpenEngine/OpenEngine/commit/e42344f5d6ce1db5fb22dfe68af41cc2f79714e9))
* **github:** prevent OAuth refresh races across workers ([#325](https://github.com/OpenEngine/OpenEngine/issues/325)) ([b2cc79b](https://github.com/OpenEngine/OpenEngine/commit/b2cc79b03aa212973060746cb16f03aca9fae5d7))
* **github:** refresh expired OAuth access tokens ([#241](https://github.com/OpenEngine/OpenEngine/issues/241)) ([1bbd8e7](https://github.com/OpenEngine/OpenEngine/commit/1bbd8e7871cff3ac2612cb98abf47055d949dc66))
* **graph:** deliver steering sent while an agent answers earlier steering ([#280](https://github.com/OpenEngine/OpenEngine/issues/280)) ([3965d55](https://github.com/OpenEngine/OpenEngine/commit/3965d5586b20ee726ef6916637850728c2cdd072))
* **graph:** forward ACP MCP servers ([#305](https://github.com/OpenEngine/OpenEngine/issues/305)) ([49c3816](https://github.com/OpenEngine/OpenEngine/commit/49c381681b3c790fa1058de9611d081477fc1b19))
* **graph:** give the run-bound MCP server the env ACP requires ([#315](https://github.com/OpenEngine/OpenEngine/issues/315)) ([d087e34](https://github.com/OpenEngine/OpenEngine/commit/d087e34bc4d8558a2622d540b4d30e563339e1f4))
* **graph:** show an agent's work while it is working ([#276](https://github.com/OpenEngine/OpenEngine/issues/276)) ([11b671a](https://github.com/OpenEngine/OpenEngine/commit/11b671abc59697e4e63cfe381d5fdbbdc1c2b873))
* hold a step's pr_url and add_comment to the run's own pull requests ([#445](https://github.com/OpenEngine/OpenEngine/issues/445)) ([c8abc6c](https://github.com/OpenEngine/OpenEngine/commit/c8abc6c98b90be1bdab12286bdd3dd77e36e4a0a))
* include PR link in Slack human review requests ([#411](https://github.com/OpenEngine/OpenEngine/issues/411)) ([d9b377e](https://github.com/OpenEngine/OpenEngine/commit/d9b377efe934280d90b7634c022cde09def6c0f7))
* isolate agent GitHub actions from personal login credentials ([#492](https://github.com/OpenEngine/OpenEngine/issues/492)) ([3efc735](https://github.com/OpenEngine/OpenEngine/commit/3efc7350dd1f7b3e07dc3c0c974377ee7c4e24f3))
* keep WorkOrders readable when a graph workflow is renamed ([#383](https://github.com/OpenEngine/OpenEngine/issues/383)) ([1b91d37](https://github.com/OpenEngine/OpenEngine/commit/1b91d37e28fae200f6649dc869fbe4641dc7e66e))
* **langgraph-acp:** authenticate the Codex compatibility turn with the API key ([#540](https://github.com/OpenEngine/OpenEngine/issues/540)) ([ad804ea](https://github.com/OpenEngine/OpenEngine/commit/ad804ea047536348261f874b137d0d1f470097eb))
* **login:** add github client id and redirect uri to engine.toml ([c8756f4](https://github.com/OpenEngine/OpenEngine/commit/c8756f4f2b8dee769f1ed3749fa7fc146216d617))
* match issue assignments using authenticated GitHub identity ([#521](https://github.com/OpenEngine/OpenEngine/issues/521)) ([085b7f1](https://github.com/OpenEngine/OpenEngine/commit/085b7f1f067ca6690f6f72594ff0079edafc2137))
* **mcp:** authenticate the MCP gateway to OE when GitHub login is required ([#488](https://github.com/OpenEngine/OpenEngine/issues/488)) ([2b4cf53](https://github.com/OpenEngine/OpenEngine/commit/2b4cf53840f3a9f8b3f0433e8792dbe0317c5054)), closes [#486](https://github.com/OpenEngine/OpenEngine/issues/486)
* migrate graph runtime tables with Alembic ([#331](https://github.com/OpenEngine/OpenEngine/issues/331)) ([66a3156](https://github.com/OpenEngine/OpenEngine/commit/66a315618479eea05989c594c1b9057f2e9b1282))
* order Slack work-order confirmation before progress ([#513](https://github.com/OpenEngine/OpenEngine/issues/513)) ([20b71c3](https://github.com/OpenEngine/OpenEngine/commit/20b71c3961c83ed04944069d1847ef90b0e64312))
* persist chat metadata across restarts ([#24](https://github.com/OpenEngine/OpenEngine/issues/24)) ([2ceb554](https://github.com/OpenEngine/OpenEngine/commit/2ceb554f4222359e97d6da6d1947f47ed8cf7e84))
* persists conversations across reboots ([#12](https://github.com/OpenEngine/OpenEngine/issues/12)) ([4d10341](https://github.com/OpenEngine/OpenEngine/commit/4d1034117daefe2e292446795dcac3387b81ada6))
* pin ACP adapter versions ([#489](https://github.com/OpenEngine/OpenEngine/issues/489)) ([23effbf](https://github.com/OpenEngine/OpenEngine/commit/23effbf979909284340b8f9360631b717dbc75f3))
* **planning:** scope milestone tools to project chats ([#127](https://github.com/OpenEngine/OpenEngine/issues/127)) ([e7428fa](https://github.com/OpenEngine/OpenEngine/commit/e7428fa64ffc364e2b6e206df1b617510dfe0630))
* preserve and expand workorder prompt formatting ([#499](https://github.com/OpenEngine/OpenEngine/issues/499)) ([06cb2fe](https://github.com/OpenEngine/OpenEngine/commit/06cb2fe427384b6541f48723958e204fa4e9a330))
* preserve durable transcripts when steering finished nodes ([#330](https://github.com/OpenEngine/OpenEngine/issues/330)) ([53b73a3](https://github.com/OpenEngine/OpenEngine/commit/53b73a32e283135c65c4e32297aafdd529a639e8))
* prevent auto-approve from approving plans and clarifications ([#319](https://github.com/OpenEngine/OpenEngine/issues/319)) ([90e6c96](https://github.com/OpenEngine/OpenEngine/commit/90e6c967c1951c7be0718dadc942aaf2cf53d9a5))
* reactivate closed workflow steps ([#75](https://github.com/OpenEngine/OpenEngine/issues/75)) ([fcc75ab](https://github.com/OpenEngine/OpenEngine/commit/fcc75ab22353a686a16f2332ec4a9bfa9b31a7f1))
* read a change-request URL one way, everywhere ([#435](https://github.com/OpenEngine/OpenEngine/issues/435)) ([f137020](https://github.com/OpenEngine/OpenEngine/commit/f137020be8ec699812d0cce11048e2cfc06c85ad))
* read structured JSON from the naming agent ([#316](https://github.com/OpenEngine/OpenEngine/issues/316)) ([eeb2b88](https://github.com/OpenEngine/OpenEngine/commit/eeb2b883f91bb4a63ec9132fb54544efa9ece8b4))
* **release:** bump engine-cli version with release-please ([#548](https://github.com/OpenEngine/OpenEngine/issues/548)) ([5949418](https://github.com/OpenEngine/OpenEngine/commit/5949418007dc3c2250565a400dc0cdb3a097ac4f))
* remove legacy human review runs ([#394](https://github.com/OpenEngine/OpenEngine/issues/394)) ([b365c8e](https://github.com/OpenEngine/OpenEngine/commit/b365c8e7896df43603c98a82be4a9a81396c6ef8))
* reopen implementation conversations through steering ([#326](https://github.com/OpenEngine/OpenEngine/issues/326)) ([1b9ed93](https://github.com/OpenEngine/OpenEngine/commit/1b9ed937a92fc4f7fe4290ecdd122d85a195d954))
* require follow-up on addressed review comments ([#441](https://github.com/OpenEngine/OpenEngine/issues/441)) ([ed4f7d9](https://github.com/OpenEngine/OpenEngine/commit/ed4f7d9bc37c9880d6eab6d73a5d68cc0e67fa22))
* require mentions for comments without active work orders ([#523](https://github.com/OpenEngine/OpenEngine/issues/523)) ([ba3fa67](https://github.com/OpenEngine/OpenEngine/commit/ba3fa67bd10e00aea3bb056e7c478a212368fc59))
* reset failed graph workflow status on implementer messages ([#342](https://github.com/OpenEngine/OpenEngine/issues/342)) ([46da81a](https://github.com/OpenEngine/OpenEngine/commit/46da81a7dc930a27f62dd056b145e2c7fa0d5ff6))
* restore graph workspace detach and reattach ([#352](https://github.com/OpenEngine/OpenEngine/issues/352)) ([bab9dd2](https://github.com/OpenEngine/OpenEngine/commit/bab9dd2687d263d6935ac3d1439be5bad2b97a1f))
* restrict change-request posting to configured hosts ([#436](https://github.com/OpenEngine/OpenEngine/issues/436)) ([59eff47](https://github.com/OpenEngine/OpenEngine/commit/59eff47fee8c61cf72fe188019b3c5a8bed91f8e))
* return review validation errors to the calling model ([#475](https://github.com/OpenEngine/OpenEngine/issues/475)) ([cb8be9e](https://github.com/OpenEngine/OpenEngine/commit/cb8be9eba98a090d63e9c965a3009b98bcc8004e))
* **runtime:** keep a step that ends in its terminal call ([#119](https://github.com/OpenEngine/OpenEngine/issues/119)) ([2898379](https://github.com/OpenEngine/OpenEngine/commit/289837917236016890fae9b47e2ef758546361e2)), closes [#105](https://github.com/OpenEngine/OpenEngine/issues/105)
* **runtime:** let the naming turn call the tools it was granted ([#311](https://github.com/OpenEngine/OpenEngine/issues/311)) ([5feacd4](https://github.com/OpenEngine/OpenEngine/commit/5feacd4f63e98837efe78e2b3aada6400c4e3721))
* **runtime:** persist interrupted agent turns ([060ac13](https://github.com/OpenEngine/OpenEngine/commit/060ac138ed6c4de8ecc78d9d9f35f051abc35ed0))
* **runtime:** read agent JSONL without the 64 KiB line limit ([#43](https://github.com/OpenEngine/OpenEngine/issues/43)) ([c87f397](https://github.com/OpenEngine/OpenEngine/commit/c87f39705a18f4423efc2886f86a29a51fb6d28d))
* **runtime:** serve a naming turn only the tools that read ([#313](https://github.com/OpenEngine/OpenEngine/issues/313)) ([3233540](https://github.com/OpenEngine/OpenEngine/commit/323354089c6d3c8ac7f6c9a29d43ab9b364c3435))
* **runtime:** tell an agent which tools it was granted ([#128](https://github.com/OpenEngine/OpenEngine/issues/128)) ([6f3ea9e](https://github.com/OpenEngine/OpenEngine/commit/6f3ea9ed513613c9a900f698cc17ed1f3bb62a6c))
* serialize steering with provider cancellation completion ([#412](https://github.com/OpenEngine/OpenEngine/issues/412)) ([86aa3e9](https://github.com/OpenEngine/OpenEngine/commit/86aa3e9c854ecfa0030e22a2009dac711ac72b69))
* show graph node activity and approvals in sidebar ([#332](https://github.com/OpenEngine/OpenEngine/issues/332)) ([775bb91](https://github.com/OpenEngine/OpenEngine/commit/775bb918b9c4c61edfff90f333d5175156f1a6f2))
* show workorder name as graph node heading ([#345](https://github.com/OpenEngine/OpenEngine/issues/345)) ([0372f50](https://github.com/OpenEngine/OpenEngine/commit/0372f503e6d96144035ce4240de496e0d5949f70))
* **site:** disable npm dependency install scripts ([#539](https://github.com/OpenEngine/OpenEngine/issues/539)) ([a772990](https://github.com/OpenEngine/OpenEngine/commit/a772990832465dfad19c330608de7c0f983b2937))
* **site:** upgrade serialize-javascript to a patched version ([#533](https://github.com/OpenEngine/OpenEngine/issues/533)) ([d7debff](https://github.com/OpenEngine/OpenEngine/commit/d7debff69dcf75134808227aeadc90e24982b60d))
* **slack:** correct checkout config and announce before execution ([#359](https://github.com/OpenEngine/OpenEngine/issues/359)) ([abef034](https://github.com/OpenEngine/OpenEngine/commit/abef034f8e19b22a95de1ba16b2b771ca84535de))
* **slack:** defer work-order announcement until after reply and prevent repo override ([#355](https://github.com/OpenEngine/OpenEngine/issues/355)) ([4ebd942](https://github.com/OpenEngine/OpenEngine/commit/4ebd94226cc84e126c3fe26bea34a289dc1d4d99))
* **slack:** remove repository parameter from concierge tool ([#358](https://github.com/OpenEngine/OpenEngine/issues/358)) ([7eff99f](https://github.com/OpenEngine/OpenEngine/commit/7eff99f12ec3f61bd464ecf7eaabd865eb18e06b))
* **slack:** request history scopes so thread replies are heard ([#354](https://github.com/OpenEngine/OpenEngine/issues/354)) ([1d93c91](https://github.com/OpenEngine/OpenEngine/commit/1d93c917fdf454b3aff4527d1ceaefdb556ca703))
* stack runner selector above auto-approve ([#341](https://github.com/OpenEngine/OpenEngine/issues/341)) ([c662ebb](https://github.com/OpenEngine/OpenEngine/commit/c662ebb41e45d7561c2cf14d2491db0e3da76cbe))
* stop announcing approvals the run answers itself ([#413](https://github.com/OpenEngine/OpenEngine/issues/413)) ([2c66472](https://github.com/OpenEngine/OpenEngine/commit/2c664726939f04baea81fea8faf1f181599b1b9a))
* structured codex object tool results, useful errors on detach ([#23](https://github.com/OpenEngine/OpenEngine/issues/23)) ([48bfda2](https://github.com/OpenEngine/OpenEngine/commit/48bfda2a84041e429cc93b95d87e1817b4ea5135))
* suppress terminal correction after steering cancellation ([#351](https://github.com/OpenEngine/OpenEngine/issues/351)) ([58da821](https://github.com/OpenEngine/OpenEngine/commit/58da8217c52a7d727ea40824d3696d2356e7a11e))
* test the latest three CLI releases on every compatibility run ([#505](https://github.com/OpenEngine/OpenEngine/issues/505)) ([5dd55fa](https://github.com/OpenEngine/OpenEngine/commit/5dd55fabb4c50fb402d03d9e6ff7973928c47091))
* tighten Slack review notifications ([#268](https://github.com/OpenEngine/OpenEngine/issues/268)) ([7ca0316](https://github.com/OpenEngine/OpenEngine/commit/7ca0316c4d6ea1feb9276c2dd74888ee200f21b7)), closes [#263](https://github.com/OpenEngine/OpenEngine/issues/263)
* upgrade remote MCP server to SDK v2 ([#482](https://github.com/OpenEngine/OpenEngine/issues/482)) ([fe4e4a6](https://github.com/OpenEngine/OpenEngine/commit/fe4e4a6a7c738becef64cdf81b71d70f8d2218cd))
* use Claude tier aliases for review models ([#490](https://github.com/OpenEngine/OpenEngine/issues/490)) ([5ada5ca](https://github.com/OpenEngine/OpenEngine/commit/5ada5cadf996289281095cc5791e653acdaaba0a))
* use server timezone in work-order progress updates ([#506](https://github.com/OpenEngine/OpenEngine/issues/506)) ([2d86571](https://github.com/OpenEngine/OpenEngine/commit/2d865715ef8ffdfdb12d17fac9b4777e1da3035f))
* use the gh CLI login for agent GitHub actions ([#495](https://github.com/OpenEngine/OpenEngine/issues/495)) ([5fe18f6](https://github.com/OpenEngine/OpenEngine/commit/5fe18f6933cfff6021b968dc4df6e17b2ec55fb3))
* verify reported pull request ownership against remote branch movement ([#470](https://github.com/OpenEngine/OpenEngine/issues/470)) ([0f9458f](https://github.com/OpenEngine/OpenEngine/commit/0f9458f75ae76bb0bed7055aeef03ab5b0aea80a))
* **web:** avoid duplicate inline approvals ([#202](https://github.com/OpenEngine/OpenEngine/issues/202)) ([b83255d](https://github.com/OpenEngine/OpenEngine/commit/b83255d0b0a1d8a0feb75029a417ea0945c45f7c))
* **web:** clean up dev server processes ([#269](https://github.com/OpenEngine/OpenEngine/issues/269)) ([6306b3d](https://github.com/OpenEngine/OpenEngine/commit/6306b3decd86a56b0bde2f6b6ff1a2f81eaceaa0))
* **web:** collapse a sidebar section by clicking its header ([#121](https://github.com/OpenEngine/OpenEngine/issues/121)) ([a0a2c02](https://github.com/OpenEngine/OpenEngine/commit/a0a2c02a833a37cf9201c0b2c92d778271fdf35a))
* **web:** compress responses without buffering event streams ([#472](https://github.com/OpenEngine/OpenEngine/issues/472)) ([e4a21da](https://github.com/OpenEngine/OpenEngine/commit/e4a21da1a96860ace985b31e2a56c4a43f1f2fbb))
* **web:** deduplicate replayed tool calls ([#134](https://github.com/OpenEngine/OpenEngine/issues/134)) ([d186f1d](https://github.com/OpenEngine/OpenEngine/commit/d186f1d76adf319b88007b6c8c6f82e1a201d0d2))
* **web:** disable dependency lifecycle scripts ([#271](https://github.com/OpenEngine/OpenEngine/issues/271)) ([9b25f34](https://github.com/OpenEngine/OpenEngine/commit/9b25f34b37b1dd149ea8bb16565fc14b7cb54959)), closes [#270](https://github.com/OpenEngine/OpenEngine/issues/270)
* **web:** keep a milestone's tooltip on the page ([#143](https://github.com/OpenEngine/OpenEngine/issues/143)) ([5c23990](https://github.com/OpenEngine/OpenEngine/commit/5c239906ddd5dd6a832d199b0098cf69f612729c))
* **web:** keep approvals on a reloaded conversation ([#79](https://github.com/OpenEngine/OpenEngine/issues/79)) ([74168a9](https://github.com/OpenEngine/OpenEngine/commit/74168a9413144ba2936ad9134a1348b2ec0f26d0))
* **web:** keep queue composer sendable ([#38](https://github.com/OpenEngine/OpenEngine/issues/38)) ([78495b9](https://github.com/OpenEngine/OpenEngine/commit/78495b983bfcf3ce5b82948cf2c7978a83dea440))
* **web:** keep review approvals in header row ([#126](https://github.com/OpenEngine/OpenEngine/issues/126)) ([442da04](https://github.com/OpenEngine/OpenEngine/commit/442da043b2587d1951ed27ea13468901a60b9086))
* **web:** keep workflow conversations in run navigation ([#68](https://github.com/OpenEngine/OpenEngine/issues/68)) ([84a2eca](https://github.com/OpenEngine/OpenEngine/commit/84a2eca7c78ddf4d5d51ab6c3a62b7239c7552f8))
* **web:** left-align milestone timeline ([#151](https://github.com/OpenEngine/OpenEngine/issues/151)) ([60631a2](https://github.com/OpenEngine/OpenEngine/commit/60631a2a6956c1bb98863421267936aa9bb79558))
* **web:** link milestone nodes to details ([#157](https://github.com/OpenEngine/OpenEngine/issues/157)) ([a97999e](https://github.com/OpenEngine/OpenEngine/commit/a97999e4436a6d30d194a243ad52376ddf90a809))
* **web:** match the new project button to its siblings ([#125](https://github.com/OpenEngine/OpenEngine/issues/125)) ([873c79d](https://github.com/OpenEngine/OpenEngine/commit/873c79d043d9d88530bea85315dfa2023b1afe87))
* **web:** open a project's plan from the rail ([#124](https://github.com/OpenEngine/OpenEngine/issues/124)) ([1f382c4](https://github.com/OpenEngine/OpenEngine/commit/1f382c4852e0ec491db1a5b9f78c2a7684d1e294))
* **web:** persist queued chat messages ([#120](https://github.com/OpenEngine/OpenEngine/issues/120)) ([a548724](https://github.com/OpenEngine/OpenEngine/commit/a548724bab1f32d56f64dad7d8f5e01900e309e7))
* **web:** persist unsent chat drafts ([#47](https://github.com/OpenEngine/OpenEngine/issues/47)) ([2243c56](https://github.com/OpenEngine/OpenEngine/commit/2243c56cfe179fca4c6004c8d8e2d494f4f4f938))
* **web:** poll graph events incrementally with cursors ([#473](https://github.com/OpenEngine/OpenEngine/issues/473)) ([b737a84](https://github.com/OpenEngine/OpenEngine/commit/b737a84f0e5d2855e99b69ed73bdd39386204e4d))
* **web:** position runner selection beside auto-approve ([#348](https://github.com/OpenEngine/OpenEngine/issues/348)) ([d27a575](https://github.com/OpenEngine/OpenEngine/commit/d27a5755d52b1ae21abd34e2fc93cbdac653211d))
* **web:** preserve archived chats on startup ([#28](https://github.com/OpenEngine/OpenEngine/issues/28)) ([78e7e24](https://github.com/OpenEngine/OpenEngine/commit/78e7e240900b11474a68828db67a218363820125))
* **web:** preserve cancellation during stream polling ([80769a2](https://github.com/OpenEngine/OpenEngine/commit/80769a22dbe9dc202ffbd6ca4f1e7faeb97452de))
* **web:** proxy /graph from the dev server ([#275](https://github.com/OpenEngine/OpenEngine/issues/275)) ([64d0d14](https://github.com/OpenEngine/OpenEngine/commit/64d0d142012c2180d9ebe71b4b0c12639979afea))
* **web:** reach the dev client under a tailnet name ([#231](https://github.com/OpenEngine/OpenEngine/issues/231)) ([d343649](https://github.com/OpenEngine/OpenEngine/commit/d343649f90213461a429e2c6b7fdec49b1b32724))
* **web:** read Claude's live credential, not the stale file beside it ([#294](https://github.com/OpenEngine/OpenEngine/issues/294)) ([6c70107](https://github.com/OpenEngine/OpenEngine/commit/6c701071fc0162eec6a5f1984fbc6a27928e52f6))
* **web:** refresh approvals in open conversations ([#86](https://github.com/OpenEngine/OpenEngine/issues/86)) ([ed9ca49](https://github.com/OpenEngine/OpenEngine/commit/ed9ca49f7cafa7549b9e76a2b552c6fb98112109))
* **web:** refresh approvals on chat traversal ([#81](https://github.com/OpenEngine/OpenEngine/issues/81)) ([f25702a](https://github.com/OpenEngine/OpenEngine/commit/f25702ae5a75715c12c15299b3395bceabfa6bbe))
* **web:** refresh new project header ([#136](https://github.com/OpenEngine/OpenEngine/issues/136)) ([a6b54ca](https://github.com/OpenEngine/OpenEngine/commit/a6b54cadd2135286a2a34c51713baa6a28a2a489))
* **web:** remove the checkout detach control from the WorkOrder overview ([#543](https://github.com/OpenEngine/OpenEngine/issues/543)) ([0c9407c](https://github.com/OpenEngine/OpenEngine/commit/0c9407cf789e1cdc50367d3f228bff867150f572))
* **web:** render approvals only under the call that raised them ([#214](https://github.com/OpenEngine/OpenEngine/issues/214)) ([04ca51b](https://github.com/OpenEngine/OpenEngine/commit/04ca51bfec402651bcaa1b910a7da636892386d4))
* **web:** replay workflow approval history ([#77](https://github.com/OpenEngine/OpenEngine/issues/77)) ([9532a28](https://github.com/OpenEngine/OpenEngine/commit/9532a288da3542a545f1d0e4ea7a8517cd3bd92f))
* **web:** right-align auto-approve dialogue on graph workflows ([#322](https://github.com/OpenEngine/OpenEngine/issues/322)) ([cca785c](https://github.com/OpenEngine/OpenEngine/commit/cca785c0a8842c2f112a5debd2dba298ee37e4b5))
* **web:** send queued message on stop ([#45](https://github.com/OpenEngine/OpenEngine/issues/45)) ([406af56](https://github.com/OpenEngine/OpenEngine/commit/406af56b409b8a5f567aee8698d9d857f9824eb6))
* **web:** show clarification context in chat ([#114](https://github.com/OpenEngine/OpenEngine/issues/114)) ([996de0b](https://github.com/OpenEngine/OpenEngine/commit/996de0b4a56ba261ab3cfa8c689d1ea0236bad24))
* **web:** show graph WorkOrder progress ([#254](https://github.com/OpenEngine/OpenEngine/issues/254)) ([8f1463b](https://github.com/OpenEngine/OpenEngine/commit/8f1463b0e30265a7fc33a85657fac9405e865962))
* **web:** show workflow titles in conversations ([#116](https://github.com/OpenEngine/OpenEngine/issues/116)) ([1d876b8](https://github.com/OpenEngine/OpenEngine/commit/1d876b880610acd752831fef24eaa95f87b3ea45))
* **web:** stream every approval transition ([#83](https://github.com/OpenEngine/OpenEngine/issues/83)) ([25ff506](https://github.com/OpenEngine/OpenEngine/commit/25ff506be972295247ee69b19f6befb02cdf3674))
* **workflow:** recover interrupted reviews ([#64](https://github.com/OpenEngine/OpenEngine/issues/64)) ([0ae51b9](https://github.com/OpenEngine/OpenEngine/commit/0ae51b917ad61b2ab508e893e9045f08996bd3e7))
* **workflow:** refresh remote base on provisioning ([#48](https://github.com/OpenEngine/OpenEngine/issues/48)) ([427d68c](https://github.com/OpenEngine/OpenEngine/commit/427d68cb2dac4230131d2cbdcb732a9a1685333b))
* **workflow:** use read-only review runners ([9864066](https://github.com/OpenEngine/OpenEngine/commit/9864066eb9a652dca9458382e3896359c15bb2cc))


### Performance Improvements

* project graph state on polling paths ([#474](https://github.com/OpenEngine/OpenEngine/issues/474)) ([a5f806f](https://github.com/OpenEngine/OpenEngine/commit/a5f806fddb62087dd3856c93eb109a0ec1bb6ef3))
* read run detail topology once per workflow ([#471](https://github.com/OpenEngine/OpenEngine/issues/471)) ([4ec776c](https://github.com/OpenEngine/OpenEngine/commit/4ec776ccd6f4a90826cc7e1e82374f5cbcd05ce7))
* **runtime:** batch run conversation reads ([#190](https://github.com/OpenEngine/OpenEngine/issues/190)) ([905dc3c](https://github.com/OpenEngine/OpenEngine/commit/905dc3c1b60f7df2ca418d452c7544a7d6a1722d))
* **sqlite:** index conversation messages ([#189](https://github.com/OpenEngine/OpenEngine/issues/189)) ([a5af015](https://github.com/OpenEngine/OpenEngine/commit/a5af0150a658b25ef0bbded88523cb635f226372))
* **web:** defer active chat restoration ([#213](https://github.com/OpenEngine/OpenEngine/issues/213)) ([a983114](https://github.com/OpenEngine/OpenEngine/commit/a9831149d60d30c5ecf71109665baa364716e2b3))
* **web:** list WorkOrders without their prose ([#218](https://github.com/OpenEngine/OpenEngine/issues/218)) ([0a8ed42](https://github.com/OpenEngine/OpenEngine/commit/0a8ed4271639ddeb4bccaacbf62d55728b50b1ce))


### Documentation

* add a roadmap for openengine ([#257](https://github.com/OpenEngine/OpenEngine/issues/257)) ([589bdc6](https://github.com/OpenEngine/OpenEngine/commit/589bdc63a0bd32101ad235411471f477419873ad))
* add Apache 2.0 license ([#154](https://github.com/OpenEngine/OpenEngine/issues/154)) ([655139e](https://github.com/OpenEngine/OpenEngine/commit/655139e2500ef71b6e55113113ca27e848d471d6))
* Add OpenEngine description to README ([#18](https://github.com/OpenEngine/OpenEngine/issues/18)) ([3bf3c47](https://github.com/OpenEngine/OpenEngine/commit/3bf3c47d6afbb28b9c4860017047fca249779e0a))
* add Slack ([b7259dc](https://github.com/OpenEngine/OpenEngine/commit/b7259dcdb918141ca23d32f014f06a6ac79fe1b4))
* Clarify database migration and remove outdated content ([bfcaad4](https://github.com/OpenEngine/OpenEngine/commit/bfcaad4ae56071e247317053f2edf5d154f3845f))
* clarify remote MCP deployment routing and health checks ([#467](https://github.com/OpenEngine/OpenEngine/issues/467)) ([238a166](https://github.com/OpenEngine/OpenEngine/commit/238a1668481f222b2ba04edac2642642ff1e5ad2))
* correct target project launch command ([#362](https://github.com/OpenEngine/OpenEngine/issues/362)) ([219c4be](https://github.com/OpenEngine/OpenEngine/commit/219c4be9930a26f85b6e993f71309f4593bf922c))
* document GitHub publishing workaround ([#33](https://github.com/OpenEngine/OpenEngine/issues/33)) ([6e42e19](https://github.com/OpenEngine/OpenEngine/commit/6e42e190cf2d94223decec2a4ac4e369678e16e6))
* **e2e:** write up the remaining browser tests as tickets ([#102](https://github.com/OpenEngine/OpenEngine/issues/102)) ([72c58c1](https://github.com/OpenEngine/OpenEngine/commit/72c58c17e1781e965a8f45c0e957bbdb86a39da8))
* langgraph-acp implementation plan ([#130](https://github.com/OpenEngine/OpenEngine/issues/130)) ([b53d12a](https://github.com/OpenEngine/OpenEngine/commit/b53d12a63d17390e01352aab779dfd687b824f75))
* Modify README to include project path in run command ([afb2a76](https://github.com/OpenEngine/OpenEngine/commit/afb2a76b5a059f437e3db36defd24828b831096a))
* plan milestones for issue 179 ([#207](https://github.com/OpenEngine/OpenEngine/issues/207)) ([8c78415](https://github.com/OpenEngine/OpenEngine/commit/8c784151ea1cb0570ab82b3d5227b6c33f3d7c97))
* plan portable distribution ([#97](https://github.com/OpenEngine/OpenEngine/issues/97)) ([aec84e8](https://github.com/OpenEngine/OpenEngine/commit/aec84e8f5812bebb97c7401e0c51a5527be30836))
* plan Temporal graph workflow integration ([#253](https://github.com/OpenEngine/OpenEngine/issues/253)) ([ad2e564](https://github.com/OpenEngine/OpenEngine/commit/ad2e564b12f970a564696825161353e1c4a84c5a))
* plan the development server ([#187](https://github.com/OpenEngine/OpenEngine/issues/187)) ([b2cee31](https://github.com/OpenEngine/OpenEngine/commit/b2cee31b9d6ef84c422e283b394f96a330ca5d21))
* Remove comments from implementation_review_graph.py ([5205a3b](https://github.com/OpenEngine/OpenEngine/commit/5205a3b601b709743583feeef20a7f3f4e4edd81))
* Remove redundant command from README ([#19](https://github.com/OpenEngine/OpenEngine/issues/19)) ([1afdf93](https://github.com/OpenEngine/OpenEngine/commit/1afdf93b5436bb0cc35958346f80f2da29d508bf))
* specify ACPNode session usage accounting ([#440](https://github.com/OpenEngine/OpenEngine/issues/440)) ([0b9fbcf](https://github.com/OpenEngine/OpenEngine/commit/0b9fbcf594e08c3ac7324fd4b72ee13029134c06))
* Update readme  ([61e207a](https://github.com/OpenEngine/OpenEngine/commit/61e207adb34a5667dbb9036b50b6241b4d13d647))
* Update Readme for v1. ([#398](https://github.com/OpenEngine/OpenEngine/issues/398)) ([d0057cf](https://github.com/OpenEngine/OpenEngine/commit/d0057cf4ae720a7452ff23531c611da28d959bd6))
* Update README to clarify installation requirements ([1379082](https://github.com/OpenEngine/OpenEngine/commit/137908293a6daa5749f531f1f8404da0c19d7071))
* update site ([#517](https://github.com/OpenEngine/OpenEngine/issues/517)) ([42791ed](https://github.com/OpenEngine/OpenEngine/commit/42791ed64f113c76e098bb1cea1a2b0f60ad5e45))
* **workflows:** plan LangGraph migration ([#117](https://github.com/OpenEngine/OpenEngine/issues/117)) ([a9e9c87](https://github.com/OpenEngine/OpenEngine/commit/a9e9c877b86de701999e0855a0ec8b77b7435948))


### Code Refactoring

* remove workstreams from the planning hierarchy ([#402](https://github.com/OpenEngine/OpenEngine/issues/402)) ([7ef3ae3](https://github.com/OpenEngine/OpenEngine/commit/7ef3ae3d970b4f8b37d80ac78b1de23c9bb5597c))
* retire the direct-CLI runners and compatibility matrix ([#532](https://github.com/OpenEngine/OpenEngine/issues/532)) ([4cd786f](https://github.com/OpenEngine/OpenEngine/commit/4cd786f8eb6c980dc1012df9ce4a5494f44a5bf5))
