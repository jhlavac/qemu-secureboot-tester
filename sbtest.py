#!/bin/python3
# Copyright (C) 2018 Red Hat
# Authors:
# - Patrick Uiterwijk <puiterwijk@redhat.com>
# - Kashyap Chamarthy <kchamart@redhat.com>
#
# Licensed under MIT License, for full text see LICENSE

from __future__ import print_function

import argparse
import threading
import glob
import os
import logging
import tempfile
import time
import shutil
import string
import subprocess
import uuid
import sys


EXIT_CODE_UNKNOWN       = 0
EXIT_CODE_SETUP_ERROR   = 1
EXIT_CODE_SHIM_ERROR    = 2
EXIT_CODE_GRUB_ERROR    = 3
EXIT_CODE_KERN_ERROR    = 4


def strip_special(line):
    return ''.join([c for c in str(line) if c in string.printable])


def run_command(cmd, stdin=None, sudo=False, **kwargs):
    logging.debug('Running command: %s', cmd)
    if sudo:
        logging.info('Sudo command running')
        cmd = ['sudo'] + cmd
    logging.debug('Stdin: %s', stdin)
    p = subprocess.Popen(cmd,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         **kwargs)
    stdout, stderr = p.communicate(stdin)
    rc = p.wait()

    logging.debug('Return code: %s', rc)
    logging.debug('Stdout: %s', stdout)
    logging.debug('Stderr: %s', stderr)

    if rc != 0:
        raise Exception('Command failed, status: %s, out: %s, err: %s'
                        % (rc, stdout, stderr))

    return stdout.decode('utf-8'), stderr.decode('utf-8')


class LoopDiskManager(object):
    numbytes = None
    dest = None
    devpath = None
    mountpath = None
    f = None

    def __init__(self, numbytes, dest):
        self.numbytes = numbytes
        self.dest = dest

    def __enter__(self):
        self.f = tempfile.NamedTemporaryFile(
                dir=os.path.dirname(self.dest),
                suffix='_hlimg',
                delete=True)
        self.f.seek(self.numbytes - 1)
        self.f.write(b'\0')
        self.f.seek(0)

        devpath, err = run_command([
                'losetup',
                '--find',
                '--show',
                self.f.name,
            ],
            sudo=True)
        self.devpath = devpath.strip()

        run_command([
                'mkfs.vfat',
                self.devpath,
            ],
            sudo=True)

        mountpath = tempfile.mkdtemp(prefix='sb_test_mnt_')
        run_command([
                'mount',
                self.devpath,
                mountpath,
            ],
            sudo=True)
        self.mountpath = mountpath

        return mountpath

    def __exit__(self, exc_type, exc_value, traceback):
        if self.mountpath:
            run_command([
                    'umount',
                    self.mountpath,
                ],
                sudo=True)
            os.rmdir(self.mountpath)

        if self.devpath is not None:
            run_command([
                    'losetup',
                    '--detach',
                    self.devpath,
                ],
                sudo=True)

        if exc_type is None:
            os.link(self.f.name, self.dest)
        self.f.close()


def generate_qemu_cmd(args):
    machinetype = 'q35'
    machinetype += ',accel=%s' % ('kvm' if args.enable_kvm else 'tcg')
    return [
        args.qemu_binary,
        '-machine', machinetype,
        '-display', 'none',
        '-no-user-config',
        '-nodefaults',
        '-m', '256',
        '-nic', 'none',
        '-smp', '2,sockets=2,cores=1,threads=1',
        '-chardev', 'pty,id=charserial1',
        '-device', 'isa-serial,chardev=charserial1,id=serial1',
        '-global', 'driver=cfi.pflash01,property=secure,value=off',
        '-drive',
        'file=%s,if=pflash,format=raw,unit=0,readonly=on' % (
            args.ovmf_binary),
        '-drive',
        'file=%s,if=pflash,format=raw,unit=1,readonly=off' % (
            os.path.join(args.workdir, 'ovmf_vars.fd')),
        '-serial', 'mon:stdio',
        '-drive',
        'file=%s,if=ide,index=0,format=raw' % (
            os.path.join(args.workdir, 'test.img')),
        '-boot', 'menu=on,order=c,strict=on']


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', help='Working directory. Default: temporary')
    parser.add_argument('--print-output', help='Print the QEMU guest output',
                        action='store_true')
    parser.add_argument('--verbose', '-v', help='Increase verbosity',
                        action='count')
    parser.add_argument('--quiet', '-q', help='Decrease verbosity',
                        action='count')
    parser.add_argument('--qemu-binary', help='QEMU binary path',
                        default='/usr/bin/qemu-system-x86_64')
    parser.add_argument('--enable-kvm', help='Enable KVM acceleration',
                        action='store_true')
    parser.add_argument('--ovmf-binary', help='OVMF secureboot code file',
                        default='/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd')
    parser.add_argument('--ovmf-template-vars', help='OVMF empty vars file',
                        default='/usr/share/edk2/ovmf/OVMF_VARS.fd')
    parser.add_argument('--shell', help='UEFI shell',
                        default='/usr/share/edk2/ovmf/Shell.efi')
    parser.add_argument('--ovmf-really-secboot',
                        help='Assume the OVMF binary is secureboot capable',
                        action='store_true')
    parser.add_argument('--ovmf-vars-really-secboot',
                        help='Assume the OVMF vars is secureboot capable',
                        action='store_true')

    parser.add_argument('--cert-to-efi-sig-list', help='c-to-esl binary',
                        default='cert-to-efi-sig-list')
    parser.add_argument('--sign-efi-sig-list', help='esl-sign binary',
                        default='sign-efi-sig-list')

    parser.add_argument('--test-signed',
                        help='Ensure the shim is trusted by pre-enrolled vars',
                        action='store_true')
    parser.add_argument('--expect-cert', action='append',
                        help='Certificate strings to expect to be loaded from moklistRT')
    parser.add_argument('shim_path', metavar='shim-path',
                        help='Specify a shim binary to test')
    parser.add_argument('grub2_path', metavar='grub2-path',
                        help='Specify a grub2 efi binary to test')
    parser.add_argument('kernel_path', metavar='kernel-path',
                        help='Specify a kernel efi binary to test')
    args = parser.parse_args()
    validate_args(args)
    return args


def validate_args(args):
    if not os.path.exists(args.shim_path):
        raise Exception('Shim path invalid')
    if not os.path.exists(args.grub2_path):
        raise Exception('Grub2 path invalid')
    if not os.path.exists(args.kernel_path):
        raise Exception('Kernel path invalid')
    if not os.path.exists(args.qemu_binary):
        raise Exception('Qemu path invalid')
    if not os.path.exists(args.ovmf_binary):
        raise Exception('OVMF code path invalid')
    if not os.path.exists(args.shell):
        raise Exception('UEFI Shell path invalid')
    if 'secboot' not in args.ovmf_binary and not args.ovmf_really_secboot:
        raise Exception('OVMF binary is likely not secureboot enabled')
    if not args.test_signed:
        if 'secboot' in args.ovmf_template_vars:
            raise Exception('OVMF template vars likely pre-enrolled. Use empty vars')
    else:
        if 'secboot' not in args.ovmf_template_vars and not args.ovmf_vars_really_secboot:
            raise Exception('OVMF vars file is likely not secureboot enrolled')

    verbosity = (args.verbose or 1) - (args.quiet or 0)
    level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    elif verbosity < 0:
        level = logging.ERROR
    logging.basicConfig(level=level)


def generate_keys(args):
    keyuuid = uuid.uuid1().hex
    keygeneration = time.time()
    keytypes = {'PK': 'Platform Key',
                'KEK': 'Key Exchange Key',
                'db': 'Database Key'}

    logging.debug('Generating keys')
    for keytype in keytypes:
        keyname = keytypes[keytype]
        run_command([
                'openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', '%s.key' % keytype,
                '-new',
                '-x509',
                '-sha256',
                '-days', '2',
                '-subj', '/CN=SBTEST %d %s/' % (keygeneration, keyname),
                '-out', '%s.crt' % keytype],
            cwd=args.workdir)

    logging.debug('Converting certs to DER')
    for keytype in keytypes:
        run_command([
                'openssl', 'x509',
                '-in', '%s.crt' % keytype,
                '-out', '%s.der' % keytype,
                '-inform', 'pem',
                '-outform', 'der'],
            cwd=args.workdir)

    logging.debug('Converting certs to ESLs')
    for keytype in keytypes:
        run_command([
                args.cert_to_efi_sig_list, '-g', keyuuid,
                '%s.crt' % keytype,
                '%s.esl' % keytype],
            cwd=args.workdir)

    logging.debug('Signing ESLs')
    # PK signs itself
    run_command([
            args.sign_efi_sig_list, '-g', keyuuid, 
            '-k', 'PK.key', '-c', 'PK.crt',
            'PK', 'PK.esl', 'PK.auth'],
        cwd=args.workdir)
    # PK signs KEK
    run_command([
            args.sign_efi_sig_list, '-g', keyuuid, 
            '-k', 'PK.key', '-c', 'PK.crt',
            'KEK', 'KEK.esl', 'KEK.auth'],
        cwd=args.workdir)
    # KEK signs DB
    run_command([
            args.sign_efi_sig_list, '-g', keyuuid, 
            '-k', 'KEK.key', '-c', 'KEK.crt',
            'db', 'db.esl', 'db.auth'],
        cwd=args.workdir)

    logging.debug('Generating db P12 file')
    run_command([
            'openssl', 'pkcs12', '-export', '-out', 'db.p12',
            '-inkey', 'db.key', '-in', 'db.crt',
            '-passout', 'pass:test',
            '-name', 'dbkey'],
        cwd=args.workdir)

    logging.debug('Generating NSS database')
    run_command(['modutil',
        '-create', '-dbdir', 'sql:%s' % args.workdir,
        '-force'])
    run_command([
        'pk12util',
        '-i', '%s/db.p12' % args.workdir,
        '-d', 'sql:%s' % args.workdir,
        '-W', 'test'])


def sign_shim(args):
    out, _ = run_command(['pesign', '-S', '-i', args.shim_path])
    if 'No signatures found.' not in out:
        raise Exception('Shim binary was pre-signed')
    run_command([
        'pesign', '--sign', '-c', 'dbkey', '-n', 'sql:%s' % args.workdir,
        '-i', args.shim_path,
        '-o', os.path.join(args.workdir,
                           'shimx64.signed.efi')])


def test_shim_signature(args):
    out, _ = run_command([
        'pesign', '-S',
        '-i', os.path.join(args.workdir, 'shimx64.signed.efi')])
    if 'No signatures found.' in out:
        raise Exception('Shim binary was not signed')


def generate_disk(args):
    with LoopDiskManager(20*1024*1024,
                         os.path.join(args.workdir, 'test.img')) as disk:
        logging.debug('Generated loopback disk at: %s', disk)
        tocopy = []
        for fname in ('db.der', 'KEK.der', 'PK.der'):
            tocopy.append((os.path.join(args.workdir, fname),
                           os.path.join(disk, fname)))

        tocopy.append((os.path.join(args.workdir, 'shimx64.signed.efi'),
                       os.path.join(disk, 'shimx64.signed.efi')))
        tocopy.append((args.grub2_path,
                       os.path.join(disk, 'grubx64.efi')))
        tocopy.append((args.kernel_path,
                       os.path.join(disk, 'kernelx64.efi')))

        for copy in tocopy:
            # This copy must run as root, because vfat does not have
            # permissions
            run_command(['cp', *copy], sudo=True)

        logging.debug('Files on test disk: %s', os.listdir(disk))


CMD_WAIT = 0
CMD_PRESSKEY = 1
CMD_SETEXITCODE = 2
CMD_LOG = 3
CMD_SENDTEXT = 4
CMD_TOGGLEMONITOR = 5
CMD_SETSTARTED = 6


current_qemu_process = None
global_timeout_timer = None
current_exit_code = 1


def timeout_reached():
    if current_qemu_process is None:
        print('Timeout reached without qemu process?')
        sys.exit(current_exit_code)
    else:
        print('Timeout reached, aborting')
        current_qemu_process.kill()
        current_qemu_process.wait()
        sys.exit(current_exit_code)


COMMON_COMMANDS = [
    (CMD_WAIT,          'char device redirected'),
    (CMD_WAIT,          'BdsDxe: No bootable option or device was found.'),
    (CMD_WAIT,          'BdsDxe: Press any key to enter the Boot Manager Menu.'),
    (CMD_SETSTARTED,),
    (CMD_TOGGLEMONITOR, True),
    (CMD_PRESSKEY,      'ret'),
]


def perform_expect(commands, sin, sout, print_out):
    global current_exit_code
    global global_timeout_timer

    commands = COMMON_COMMANDS + commands

    vmstarted = False
    monitormode = False

    while len(commands) > 0:
        global_timeout_timer = None
        if vmstarted:
            global_timeout_timer = threading.Timer(10.0, timeout_reached)
            global_timeout_timer.start()

        cmd = commands[0]
        opcode = cmd[0]
        args = cmd[1:]
        commands = commands[1:]
        if opcode == CMD_PRESSKEY:
            assert monitormode, "CMD_PRESSKEY is only valid in monitor mode"
            key = args[0].encode('ascii')
            repeat = 1
            if len(args) >= 2:
                repeat = args[1]
            logging.debug('Sending key %s %d times', key, repeat)
            while repeat > 0:
                sin.write(b'sendkey %s\n' % key)
                sin.flush()
                repeat -= 1
        elif opcode == CMD_WAIT:
            needle = args[0].encode('ascii')
            logging.debug('Waiting for %s', needle)
            buf = b''
            while True:
                read = sout.read(1)
                if len(read) != 1:
                    raise Exception('Error reading')
                if print_out:
                    print(strip_special(read), end='')
                buf += read
                if needle in buf:
                    logging.debug('Found expected string')
                    break
        elif opcode == CMD_SETEXITCODE:
            code = args[0]
            logging.debug('Setting future exit code to %d', code)
            current_exit_code = code
        elif opcode == CMD_LOG:
            logging.log(*args)
        elif opcode == CMD_SENDTEXT:
            assert not monitormode, "CMD_PRESSKEY is only valid in plain text mode"
            text = args[0].encode('ascii')
            logging.debug('Sending text %s', text)
            sin.write(text)
            sin.flush()
        elif opcode == CMD_TOGGLEMONITOR:
            if monitormode != args[0]:
                logging.debug('Toggling monitor mode to %s', args[0])
                sin.write(b'\x01')
                sin.write(b'c')
                sin.flush()
                monitormode = args[0]
                if monitormode:
                    # Verify that we entered monitor mode
                    logging.debug('Verifying we entered monitor mode')
                    commands = [
                        (CMD_WAIT, 'QEMU '),
                        (CMD_WAIT, '(qemu) '),
                    ] + commands
        elif opcode == CMD_SETSTARTED:
            logging.debug('VM Marked as started, timer activating')
            vmstarted = True
        else:
            raise Exception('Invalid command opcode %d (args %s)', opcode, args)

        if global_timeout_timer is not None:
            global_timeout_timer.cancel()
            global_timeout_timer = None

    logging.debug('Reached end of command sequence')


def run_expect(args, commands):
    global current_qemu_process
    global global_timeout_timer

    cmd = generate_qemu_cmd(args)
    logging.debug('Running QEMU, command: %s', ' '.join(cmd))
    p = subprocess.Popen(cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    current_qemu_process = p
    logging.debug('Setting QEMU process: %s', p)

    exc = None
    try:
        perform_expect(commands, p.stdin, p.stdout, args.print_output)
    except Exception as e:
        logging.exception('Error processing request')
        exc = e
    finally:
        logging.debug('Killing qemu process')
        p.kill()
        p.wait()
    logging.debug('Clearing QEMU process')
    current_qemu_process = None
    if exc is not None:
        if global_timeout_timer is not None:
            global_timeout_timer.cancel()
        sys.exit(current_exit_code)


def enroll_keys(args):
    shutil.copy(args.ovmf_template_vars,
                os.path.join(args.workdir, 'ovmf_vars.fd'))
    if args.test_signed:
        logging.debug('Assuming OVMF vars are enrolled')
    else:
        logging.debug("Starting VM to process enrollment")
        cmds = [
            # Browsing to Secure Boot Management
            (CMD_WAIT,      'Select Language'),
            (CMD_PRESSKEY,  'down'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'iSCSI Configuration'),
            (CMD_PRESSKEY,  'down', 2),
            (CMD_PRESSKEY,  'ret'),
            # Enabling custom secure boot
            (CMD_WAIT,      'Secure Boot Mode'),
            (CMD_PRESSKEY,  'down'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_PRESSKEY,  'down'),
            (CMD_PRESSKEY,  'ret'),
            # Browsing to Secure Boot Key Management
            (CMD_PRESSKEY,  'down'),
            (CMD_PRESSKEY,  'ret'),
            # Enroll db key
            (CMD_WAIT,      'DBT Options'),
            (CMD_PRESSKEY,  'down', 2),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll Signature'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll Signature Using File'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'NO VOLUME LABEL'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'db.der'),
            (CMD_PRESSKEY,  'down', 3),  # db.der
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'db.der'),
            (CMD_WAIT,      'Commit Changes'),
            (CMD_PRESSKEY,  'down', 2),  # Commit Changes
            (CMD_PRESSKEY,  'ret'),
            # Enroll KEK, cursor still at DB key
            (CMD_WAIT,      'DBT Options'),
            (CMD_PRESSKEY,  'up'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll KEK'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll KEK using File'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'NO VOLUME LABEL'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'KEK.der'),
            (CMD_PRESSKEY,  'down', 4),  # KEK.der
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'KEK.der'),
            (CMD_WAIT,      'Commit Changes'),
            (CMD_PRESSKEY,  'down', 2),  # Commit Changes
            (CMD_PRESSKEY,  'ret'),
            # Enroll PK, cursor still at KEK
            (CMD_WAIT,      'DBT Options'),
            (CMD_PRESSKEY,  'up'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll PK'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'Enroll PK Using File'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'NO VOLUME LABEL'),
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'PK.der'),
            (CMD_PRESSKEY,  'down', 5),  # PK.der
            (CMD_PRESSKEY,  'ret'),
            (CMD_WAIT,      'PK.der'),
            (CMD_WAIT,      'Commit Changes'),
            (CMD_PRESSKEY,  'down'),  # Commit Changes
            (CMD_PRESSKEY,  'ret'),
            # Browse to secure boot config
            (CMD_WAIT,      'DBT Options'),
            (CMD_PRESSKEY,  'esc'),
            (CMD_WAIT,      'Current Secure Boot State'),
            (CMD_WAIT,      'Enabled'),
        ]
        run_expect(args, cmds)


def test_boot(args):
    # Okay, that was all well and good... Let's now actually test this stuff!
    logging.debug("Starting VM to attempt boot")
    cmds = [
        # Browse to "Boot from file"
        (CMD_WAIT,          'Select Language'),
        (CMD_PRESSKEY,      'down', 3),
        (CMD_PRESSKEY,      'ret'),
        (CMD_WAIT,          'Boot From File'),
        (CMD_PRESSKEY,      'down', 3),
        (CMD_PRESSKEY,      'ret'),
        (CMD_WAIT,          'NO VOLUME LABEL'),
        (CMD_PRESSKEY,      'ret'),
        # First attempt to start grubx64.efi, should drop us back
        (CMD_WAIT,          'kernelx64.efi'),
        (CMD_PRESSKEY,      'down', 4),
        (CMD_PRESSKEY,      'ret'),
        # Now attempt to start kernelx64.efi, should still not work. Cursor at grubx64.efi
        (CMD_WAIT,          'kernelx64.efi'),
        (CMD_PRESSKEY,      'down'),
        (CMD_PRESSKEY,      'ret'),
        # Now attempt to start shimx64.efi, should start.
        (CMD_WAIT,          'kernelx64.efi'),
        (CMD_SETEXITCODE,   EXIT_CODE_SHIM_ERROR),
        (CMD_PRESSKEY,      'up', 2),
        (CMD_PRESSKEY,      'ret'),
        # We should now be at the grub2 prompt
        (CMD_WAIT,          'grub>'),
        (CMD_SETEXITCODE,   EXIT_CODE_GRUB_ERROR),
        (CMD_LOG,           logging.INFO, "GRUB2 started correctly"),
        # Grub started!
        (CMD_TOGGLEMONITOR, False),
        (CMD_SENDTEXT,      'linuxefi /kernelx64.efi debug text console=tty0 console=ttyS0,115200n8\n'),
        (CMD_PRESSKEY, 'ret'),
        (CMD_SENDTEXT,      'boot\n'),
        # The Linux kernel should now be starting
        (CMD_WAIT,          'Command line: BOOT_IMAGE=/kernelx64.efi'),
        (CMD_WAIT,          'BIOS-provided physical RAM map:'),
        (CMD_SETEXITCODE,   EXIT_CODE_KERN_ERROR),
        (CMD_LOG,           logging.INFO, "Kernel started correctly"),
        # Let's verify that the built-in certs are parsed
        (CMD_WAIT,          'Loaded X.509 cert'),
        # Let's verify that the database key gets linked to the system keyring
        (CMD_WAIT,          "EFI: Loaded cert 'SBTEXT "),
    ]
    for cert in args.expect_cert:
        cmds.append((CMD_WAIT, "EFI: Loaded cert '%s' linked to '.system_keyring'"))
    run_expect(args, cmds)

def main():
    args = parse_args()
    temp_workdir = False
    if not args.workdir:
        temp_workdir = True
        args.workdir = tempfile.mkdtemp(prefix='sb_test_workdir_')
        logging.debug('Working directory: %s', args.workdir)

    if args.test_signed:
        # Assume the shim binary is fully signed
        logging.debug('Assuming shim is fully signed')
        shutil.copy(args.shim_path,
                    os.path.join(args.workdir, 'shimx64.signed.efi'))
    else:
        generate_keys(args)
        sign_shim(args)
        pass
    test_shim_signature(args)
    generate_disk(args)
    enroll_keys(args)
    test_boot(args)

    if temp_workdir:
        logging.debug('Deleting temporary directory')
        shutil.rmtree(args.workdir)


if __name__ == '__main__':
    main()
