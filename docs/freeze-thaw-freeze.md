# Freeze → Thaw → Freeze

## Demo
```bash
python demos/goblin_freeze_thaw_freeze.py --images-dir /tmp/pdum-freeze-thaw-freeze
```
- Launches a stateful counter goblin, freezes it once, thaws a clone, then immediately tries to freeze the clone.
- The script prints the CRIU log path plus the failing log tail so you can inspect the mount-parent error.

## Expected failure
```
Attempting to freeze the restored goblin (expected to fail)...
Second freeze failed as expected: CRIU dump failed with exit code 1
Tail of /tmp/pdum-freeze-thaw-freeze/second-freeze/second-freeze.<pid>.log:
    (00.002961) mnt: Inspecting sharing on 920 shared_id 0 master_id 0 (@./proc)
    (00.002962) mnt: Inspecting sharing on 919 shared_id 0 master_id 30 (@./boot/efi)
    (00.002964) Error (criu/mount.c:1088): mnt: Mount 919 ./boot/efi (master_id: 30 shared_id: 0) has unreachable sharing. Try --enable-external-masters.
    (00.003033) Error (criu/cr-dump.c:2111): Dumping FAILED.
```
CRIU walks the restored `/proc/<pid>/mountinfo` tree and reports `unreachable sharing` because the restored namespace references mount masters that only exist inside the helper-owned namespace; the host-side dumper does not see those masters and aborts.

## Mount snapshot (host)
```
sysfs on /sys type sysfs (rw,nosuid,nodev,noexec,relatime)
proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)
udev on /dev type devtmpfs (rw,nosuid,relatime,size=62646836k,nr_inodes=15661709,mode=755,inode64)
devpts on /dev/pts type devpts (rw,nosuid,noexec,relatime,gid=5,mode=620,ptmxmode=000)
tmpfs on /run type tmpfs (rw,nosuid,nodev,noexec,relatime,size=12551372k,mode=755,inode64)
/dev/nvme0n1p2 on / type ext4 (rw,relatime,errors=remount-ro)
tmpfs on /dev/shm type tmpfs (rw,nosuid,nodev,inode64)
cgroup2 on /sys/fs/cgroup type cgroup2 (rw,nosuid,nodev,noexec,relatime)
/dev/nvme0n1p1 on /boot/efi type vfat (rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=iso8859-1,shortname=mixed,errors=remount-ro)
tmpfs on /run/user/1000 type tmpfs (rw,nosuid,nodev,relatime,size=12551372k,nr_inodes=3137843,mode=700,uid=1000,gid=1000,inode64)
```
Mount namespaces restored by CRIU inherit bind mounts created by `criu-ns` rather than mounts that live directly under this host table, which is why the second dump reports unreachable parent/master mounts.

## Failure mechanics
- First freeze captures a consistent `mountinfo` tree whose parent/child IDs refer to the host mounts shown above; these IDs plus master IDs are recorded in the images.
- Restore uses `criu-ns` to spin up a detached mount namespace and reconstruct the tree, but many bind mounts now hang off helper-owned parents or master mount points (e.g., temporary tmpfs or `nsfs` bind targets) that do not exist on the host anymore.
- After thaw, the goblin lives inside that detached namespace; when `criu dump` runs again from the host namespace it inspects `/proc/<pid>/mountinfo`, sees parent/master IDs that point to helper-created mounts, and cannot map them back to any mount it controls.
- The dump helper therefore emits `Mount <id> <path> (master_id: N …) has unreachable sharing. Try --enable-external-masters.` before aborting, which matches the tail printed by the demo and leaves the second image directory empty.
- Because the issue stems from unparented/unreachable mount masters, the only reliable workaround today is to avoid the freeze → thaw → freeze workflow until CRIU grows support for capturing and reparenting those helper mounts.
