export declare function parentAlive(probe: (pid: number) => void, pid: number): boolean;
export declare function checkParent(
  probe: (pid: number, signal: number | string) => void,
  pid: number,
  killGroup: (signal: string) => void,
): "alive" | "dead";
