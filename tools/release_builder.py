#!/usr/bin/python

import base64
import difflib
import json
import logging
import os
import os.path
import pprint
import re
import shutil
import sys
import tempfile
import zipfile

try:
    from http.client import HTTPSConnection
    from urllib.request import URLopener
    from urllib.request import urlopen
except ImportError:
    # Python 2
    from httplib import HTTPSConnection
    from urllib import URLopener
    from urllib2 import urlopen

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(message)s")


class UniverseReleaseBuilder(object):

    def __init__(self, package_version, stub_universe_url,
                 commit_desc = '',
                 min_dcos_release_version = os.environ.get('MIN_DCOS_RELEASE_VERSION', '1.7'),
                 http_release_server = os.environ.get('HTTP_RELEASE_SERVER', 'https://downloads.mesosphere.com'),
                 s3_release_bucket = os.environ.get('S3_RELEASE_BUCKET', 'downloads.mesosphere.io'),
                 release_dir_path = os.environ.get('RELEASE_DIR_PATH', '')): # default set below
        self._dry_run = os.environ.get('DRY_RUN', '')
        name_match = re.match('.+/stub-universe-(.+).zip$', stub_universe_url)
        if not name_match:
            raise Exception('Unable to extract package name from stub universe URL. ' +
                            'Expected filename of form \'stub-universe-[pkgname].zip\'')
        self._pkg_name = name_match.group(1)
        if not release_dir_path:
            release_dir_path = self._pkg_name + '/assets'
        self._pkg_version = package_version
        self._commit_desc = commit_desc
        self._stub_universe_url = stub_universe_url
        self._min_dcos_release_version = min_dcos_release_version

        self._pr_title = 'Release {} {} (automated commit)\n\n'.format(
            self._pkg_name, self._pkg_version)
        self._release_artifact_http_dir = '{}/{}/{}'.format(
            http_release_server, release_dir_path, self._pkg_version)
        self._release_artifact_s3_dir = 's3://{}/{}/{}'.format(
            s3_release_bucket, release_dir_path, self._pkg_version)

        # complain early about any missing envvars...
        # avoid uploading a bunch of stuff to prod just to error out later:
        if not 'GITHUB_TOKEN' in os.environ:
            raise Exception('GITHUB_TOKEN is required: Credential to create a PR against Universe')
        encoded_tok = base64.encodestring(os.environ['GITHUB_TOKEN'].encode('utf-8'))
        self._github_token = encoded_tok.decode('utf-8').rstrip('\n')
        if not 'AWS_ACCESS_KEY_ID' in os.environ or not 'AWS_SECRET_ACCESS_KEY' in os.environ:
            raise Exception('AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required: '
                            + 'Credentials to prod AWS for uploading release artifacts')


    def _run_cmd(self, cmd, dry_run_return = 0):
        if self._dry_run:
            logger.info('[DRY RUN] {}'.format(cmd))
            return dry_run_return
        else:
            logger.info(cmd)
            return os.system(cmd)

    def _download_unpack_stub_universe(self, scratchdir):
        local_zip_path = os.path.join(scratchdir, self._stub_universe_url.split('/')[-1])
        result = urlopen(self._stub_universe_url)
        dlfile = open(local_zip_path, 'wb')
        dlfile.write(result.read())
        dlfile.flush()
        dlfile.close()
        zipin = zipfile.ZipFile(local_zip_path, 'r')
        badfile = zipin.testzip()
        if badfile:
            raise Exception('Bad file {} in downloaded {} => {}'.format(
                badfile, self._stub_universe_url, local_zip_path))
        zipin.extractall(scratchdir)
        # check for stub-universe-pkgname/repo/packages/P/pkgname/0/:
        pkgdir_path = os.path.join(
            scratchdir,
            'stub-universe-{}'.format(self._pkg_name),
            'repo',
            'packages',
            self._pkg_name[0].upper(),
            self._pkg_name,
            '0')
        if not os.path.isdir(pkgdir_path):
            raise Exception('Didn\'t find expected path {} after unzipping {}'.format(
                pkgdir_path, local_zip_path))
        os.unlink(local_zip_path)
        return pkgdir_path


    def _update_file_content(self, path, orig_content, new_content, showdiff=True):
        if orig_content == new_content:
            logger.info('No changes detected in {}'.format(path))
            # no-op
        else:
            if showdiff:
                logger.info('Applied templating changes to {}:'.format(path))
                logger.info('\n'.join(difflib.ndiff(orig_content.split('\n'), new_content.split('\n'))))
            else:
                logger.info('Applied templating changes to {}'.format(path))
            newfile = open(path, 'w')
            newfile.write(new_content)
            newfile.flush()
            newfile.close()


    def _update_package_get_artifact_source_urls(self, pkgdir):
        # replace package.json:version (smart replace)
        path = os.path.join(pkgdir, 'package.json')
        packagingVersion = '3.0'
        if self._min_dcos_release_version == '0':
            minDcosReleaseVersion = None
        else:
            minDcosReleaseVersion = self._min_dcos_release_version
        logger.info('[1/2] Setting version={}, packagingVersion={}, minDcosReleaseVersion={} in {}'.format(
            self._pkg_version, packagingVersion, minDcosReleaseVersion, path))
        orig_content = open(path, 'r').read()
        content_json = json.loads(orig_content)
        content_json['version'] = self._pkg_version
        content_json['packagingVersion'] = packagingVersion
        if minDcosReleaseVersion:
            content_json['minDcosReleaseVersion'] = minDcosReleaseVersion
        # dumps() adds trailing space, fix that:
        new_content_lines = json.dumps(content_json, indent=2, sort_keys=True).split('\n')
        new_content = '\n'.join([line.rstrip() for line in new_content_lines]) + '\n'
        logger.info(new_content)
        # don't bother showing diff, things get rearranged..
        self._update_file_content(path, orig_content, new_content, showdiff=False)

        # we expect the artifacts to share the same directory prefix as the stub universe zip itself:
        original_artifact_prefix = '/'.join(self._stub_universe_url.split('/')[:-1])
        logger.info('[2/2] Replacing artifact prefix {} with {}'.format(
            original_artifact_prefix, self._release_artifact_http_dir))
        original_artifact_urls = []
        for filename in os.listdir(pkgdir):
            path = os.path.join(pkgdir, filename)
            orig_content = open(path, 'r').read()
            found = re.findall('({}/.+)\"'.format(original_artifact_prefix), orig_content)
            original_artifact_urls += found
            new_content = orig_content.replace(original_artifact_prefix, self._release_artifact_http_dir)
            self._update_file_content(path, orig_content, new_content)
        return original_artifact_urls


    def _copy_artifacts_s3(self, scratchdir, original_artifact_urls):
        # before we do anything else, verify that the upload directory doesn't already exist, to
        # avoid automatically stomping on a previous release. if you *want* to do this, you must
        # manually delete the destination directory first. (and redirect stdout to stderr)
        cmd = 'aws s3 ls --recursive {} 1>&2'.format(self._release_artifact_s3_dir)
        ret = self._run_cmd(cmd, 1)
        if ret == 0:
            raise Exception('Release artifact destination already exists. ' +
                            'Refusing to continue until destination has been manually removed:\n' +
                            'Do this: aws s3 rm --dryrun --recursive {}'.format(self._release_artifact_s3_dir))
        elif ret > 256:
            raise Exception('Failed to check artifact destination presence (code {}). Bad AWS credentials? Exiting early.'.format(ret))
        logger.info('Destination {} doesnt exist, proceeding...'.format(self._release_artifact_s3_dir))

        for i in range(len(original_artifact_urls)):
            progress = '[{}/{}]'.format(i + 1, len(original_artifact_urls))
            src_url = original_artifact_urls[i]
            filename = src_url.split('/')[-1]

            local_path = os.path.join(scratchdir, filename)
            dest_s3_url = '{}/{}'.format(self._release_artifact_s3_dir, filename)

            # TODO: this currently downloads the file via http, then uploads it via 'aws s3 cp'.
            # copy directly from src bucket to dest bucket via 'aws s3 cp'? problem: different credentials

            # download the artifact (dev s3, via http)
            if self._dry_run:
                # create stub file to make 'aws s3 cp --dryrun' happy:
                logger.info('[DRY RUN] {} Downloading {} to {}'.format(progress, src_url, local_path))
                stub = open(local_path, 'w')
                stub.write('stub')
                stub.flush()
                stub.close()
                logger.info('[DRY RUN] {} Uploading {} to {}'.format(progress, local_path, dest_s3_url))
                ret = os.system('aws s3 cp --dryrun --acl public-read {} {} 1>&2'.format(
                    local_path, dest_s3_url))
            else:
                # download the artifact (http url referenced in package)
                logger.info('{} Downloading {} to {}'.format(progress, src_url, local_path))
                URLopener().retrieve(src_url, local_path)
                # re-upload the artifact (prod s3, via awscli)
                logger.info('{} Uploading {} to {}'.format(progress, local_path, dest_s3_url))
                ret = os.system('aws s3 cp --acl public-read {} {} 1>&2'.format(
                    local_path, dest_s3_url))
            if not ret == 0:
                raise Exception(
                    'Failed to upload {} to {}. '.format(local_path, dest_s3_url) +
                    'Partial release directory may need to be cleared manually before retrying. Exiting early.')
            os.unlink(local_path)


    def _create_universe_branch(self, scratchdir, pkgdir):
        branch = 'automated/release_{}_{}_{}'.format(
            self._pkg_name, self._pkg_version, base64.b64encode(os.urandom(4)).decode('utf-8').rstrip('='))
        # check out the repo, create a new local branch:
        ret = os.system(' && '.join([
            'cd {}'.format(scratchdir),
            'git clone --depth 1 --branch version-3.x git@github.com:mesosphere/universe',
            'cd universe',
            'git config --local user.email jenkins@mesosphere.com',
            'git config --local user.name release_builder.py',
            'git checkout -b {}'.format(branch)]))
        if not ret == 0:
            raise Exception(
                'Failed to create local Universe git branch {}. '.format(branch) +
                'Note that any release artifacts were already uploaded to {}, which must be manually deleted before retrying.'.format(self._release_artifact_s3_dir))
        universe_repo = os.path.join(scratchdir, 'universe')
        repo_pkg_base = os.path.join(
            universe_repo,
            'repo',
            'packages',
            self._pkg_name[0].upper(),
            self._pkg_name)
        # find the prior release number:
        lastnum = -1
        for filename in os.listdir(repo_pkg_base):
            if not os.path.isdir(os.path.join(repo_pkg_base, filename)):
                continue
            try:
                num = int(filename)
            except:
                continue
            if num > lastnum:
                lastnum = num
        last_repo_pkg = os.path.join(repo_pkg_base, str(lastnum))
        this_repo_pkg = os.path.join(repo_pkg_base, str(lastnum + 1))
        # copy the stub universe contents into a new release number, while collecting changes:
        os.makedirs(this_repo_pkg)
        removedfiles = os.listdir(last_repo_pkg)
        addedfiles = []
        filediffs = {}
        for filename in os.listdir(pkgdir):
            if not os.path.isfile(os.path.join(pkgdir, filename)):
                continue
            shutil.copyfile(os.path.join(pkgdir, filename), os.path.join(this_repo_pkg, filename))
            if filename in removedfiles:
                # file exists in both new and old: calculate diff
                removedfiles.remove(filename)
                oldfile = open(os.path.join(last_repo_pkg, filename), 'r')
                newfile = open(os.path.join(this_repo_pkg, filename), 'r')
                filediffs[filename] = ''.join(difflib.unified_diff(
                    oldfile.readlines(), newfile.readlines(),
                    fromfile='{}/{}'.format(lastnum, filename),
                    tofile='{}/{}'.format(lastnum + 1, filename)))
            else:
                addedfiles.append(filename)
        # create a user-friendly diff for use in the commit message:
        resultlines = [
            'Changes since revision {}:\n'.format(lastnum),
            '{} files added: [{}]\n'.format(len(addedfiles), ', '.join(addedfiles)),
            '{} files removed: [{}]\n'.format(len(removedfiles), ', '.join(removedfiles)),
            '{} files changed:\n\n'.format(len(filediffs))]
        if self._commit_desc:
            resultlines.insert(0, 'Description:\n{}\n\n'.format(self._commit_desc))
        # surround diff description with quotes to ensure formatting is preserved:
        resultlines.append('```\n')
        filediff_names = list(filediffs.keys())
        filediff_names.sort()
        for filename in filediff_names:
            resultlines.append(filediffs[filename])
        resultlines.append('```\n')
        commitmsg_path = os.path.join(scratchdir, 'commitmsg.txt')
        commitmsg_file = open(commitmsg_path, 'w')
        commitmsg_file.write(self._pr_title)
        commitmsg_file.writelines(resultlines)
        commitmsg_file.flush()
        commitmsg_file.close()
        # commit the change and push the branch:
        cmds = ['cd {}'.format(os.path.join(scratchdir, 'universe')),
                'git add .',
                'git commit -q -F {}'.format(commitmsg_path)]
        if self._dry_run:
            # ensure the debug goes to stderr...:
            cmds.append('git show -q HEAD 1>&2')
        else:
            cmds.append('git push origin {}'.format(branch))
        ret = os.system(' && '.join(cmds))
        if not ret == 0:
            raise Exception(
                'Failed to push git branch {} to Universe. '.format(branch) +
                'Note that any release artifacts were already uploaded to {}, which must be manually deleted before retrying.'.format(self._release_artifact_s3_dir))
        return (branch, commitmsg_path)


    def _create_universe_pr(self, branch, commitmsg_path):
        if self._dry_run:
            logger.info('[DRY RUN] Skipping creation of PR against branch {}'.format(branch))
            return None
        headers = {
            'User-Agent': 'release_builder.py',
            'Content-Type': 'application/json',
            'Authorization': 'Basic {}'.format(self._github_token)}
        payload = {
            'title': self._pr_title,
            'head': branch,
            'base': 'version-3.x',
            'body': open(commitmsg_path).read()}
        conn = HTTPSConnection('api.github.com')
        conn.set_debuglevel(999)
        conn.request(
            'POST',
            '/repos/mesosphere/universe/pulls',
            body = json.dumps(payload).encode('utf-8'),
            headers = headers)
        return conn.getresponse()


    def release_zip(self):
        scratchdir = tempfile.mkdtemp(prefix='stub-universe-tmp')
        pkgdir = self._download_unpack_stub_universe(scratchdir)
        original_artifact_urls = self._update_package_get_artifact_source_urls(pkgdir)
        self._copy_artifacts_s3(scratchdir, original_artifact_urls)
        (branch, commitmsg_path) = self._create_universe_branch(scratchdir, pkgdir)
        return self._create_universe_pr(branch, commitmsg_path)


def print_help(argv):
    logger.info('Syntax: {} <package-version> <stub-universe-url> [commit message]'.format(argv[0]))
    logger.info('  Example: $ {} 1.2.3-4.5.6 https://example.com/path/to/stub-universe-kafka.zip'.format(argv[0]))
    logger.info('Required credentials in env:')
    logger.info('- AWS S3: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY')
    logger.info('- Github (Personal Access Token): GITHUB_TOKEN')
    logger.info('Required CLI programs:')
    logger.info('- git')
    logger.info('- aws')


def main(argv):
    if len(argv) < 3:
        print_help(argv)
        return 1
    # the package version:
    package_version = argv[1]
    # url where the stub universe is located:
    stub_universe_url = argv[2].rstrip('/')
    # commit comment, if any:
    commit_desc = ' '.join(argv[3:])
    if commit_desc:
        comment_info = '\nCommit Message:  {}'.format(commit_desc)
    else:
        comment_info = ''
    logger.info('''###
Release Version: {}
Universe URL:    {}{}
###'''.format(package_version, stub_universe_url, comment_info))

    builder = UniverseReleaseBuilder(package_version, stub_universe_url, commit_desc)
    response = builder.release_zip()
    if not response:
        # print the PR location as stdout for use upstream (the rest is all stderr):
        print('[DRY RUN] The pull request URL would appear here.')
        return 0
    if response.status < 200 or response.status >= 300:
        logger.error('Got {} response to PR creation request:'.format(response.status))
        logger.error('Response:')
        logger.error(pprint.pformat(response.read()))
        logger.error('You will need to manually create the PR against the branch that was pushed above.')
        return -1
    logger.info('---')
    logger.info('Created pull request for version {} (PTAL):'.format(package_version))
    # print the PR location as stdout for use upstream (the rest is all stderr):
    print(json.loads(response.read().decode('utf-8'))['html_url'])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
