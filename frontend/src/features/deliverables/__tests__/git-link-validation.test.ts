import { describe, expect, it } from "vitest";
import { normalizeGitDeliveryUrl } from "../constants";

describe("Git 交付链接校验", () => {
  it.each([
    [
      " https://github.com/org/repo/pull/42/ ",
      "https://github.com/org/repo/pull/42",
    ],
    [
      "https://gitee.com/org/repo/pulls/42",
      "https://gitee.com/org/repo/pulls/42",
    ],
    [
      "https://gitlab.com/group/subgroup/repo/-/merge_requests/42/",
      "https://gitlab.com/group/subgroup/repo/-/merge_requests/42",
    ],
    [
      "https://github.com/org/repo/commit/ABCDEF1",
      "https://github.com/org/repo/commit/abcdef1",
    ],
    [
      "https://gitee.com/org/repo/commit/0123456789ABCDEF0123456789ABCDEF01234567/",
      "https://gitee.com/org/repo/commit/0123456789abcdef0123456789abcdef01234567",
    ],
    [
      "https://gitlab.com/group/repo/-/commit/ABC12345",
      "https://gitlab.com/group/repo/-/commit/abc12345",
    ],
  ])("规范化 %s", (input, expected) => {
    expect(normalizeGitDeliveryUrl(input)).toBe(expected);
  });

  it.each([
    "http://github.com/org/repo/pull/42",
    "https://github.com:443/org/repo/pull/42",
    "https://user@github.com/org/repo/pull/42",
    "https://github.com.evil.example/org/repo/pull/42",
    "https://github.com/org/repo/pull/42?diff=split",
    "https://gitee.com/org/repo/pulls/42#note",
    "https://gitlab.com/group/repo/-/merge_requests/42/diffs",
    "https://gitlab.com/group//repo/-/merge_requests/42",
    "https://github.com/org/repo/commit/abcdef",
    "https://github.com/org/repo/commit/abcdefg",
    "https://github.com/org/repo/commit/12345678901234567890123456789012345678901",
    "https://gitlab.com/repo/-/commit/abcdef1",
    "https://github.com/org/%2e%2e/pull/42",
    "https://github.com/org/foo/../repo/pull/42",
    "https://github.com/org/%ZZ/pull/42",
  ])("拒绝 %s", (input) => {
    expect(normalizeGitDeliveryUrl(input)).toBeNull();
  });
});
