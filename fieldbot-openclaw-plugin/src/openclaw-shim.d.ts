// Build-time surface used when the OpenClaw host package is not installed in
// this source checkout. Runtime imports are resolved by the OpenClaw gateway.
// This plugin is additionally validated against OpenClaw 2026.7.1-2.
declare module "openclaw/plugin-sdk/core" {
  export function jsonResult(value: unknown): unknown;
}

declare module "openclaw/plugin-sdk/tool-plugin" {
  // Only the fields this plugin touches. `register` is a plain writable
  // property on the object `definePluginEntry` returns, which is what lets us
  // extend it with plugin commands without rebuilding the entry.
  export type ToolPluginEntry = {
    id: string;
    name: string;
    description: string;
    register?: (api: unknown) => void;
  };
  export function defineToolPlugin(definition: unknown): ToolPluginEntry;
}
