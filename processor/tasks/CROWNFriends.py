import law
import luigi
import os
from CROWNBuildFriend import CROWNBuildFriend
from CROWNRun import CROWNRun
import tarfile
import subprocess
import time
from framework import console
from framework import HTCondorWorkflow
from law.config import Config
from helpers.helpers import create_abspath

law.contrib.load("wlcg")


class CROWNFriends(HTCondorWorkflow, law.LocalWorkflow):
    """
    Gather and compile CROWN with the given configuration
    """

    output_collection_cls = law.NestedSiblingFileCollection

    all_sampletypes = luigi.ListParameter(significant=False)
    all_eras = luigi.ListParameter(significant=False)
    scopes = luigi.ListParameter()
    shifts = luigi.Parameter()
    analysis = luigi.Parameter()
    friend_config = luigi.Parameter()
    config = luigi.Parameter(significant=False)
    friend_name = luigi.Parameter()
    nick = luigi.Parameter()
    sampletype = luigi.Parameter()
    era = luigi.Parameter()
    production_tag = luigi.Parameter()
    files_per_task = luigi.IntParameter()

    def htcondor_output_directory(self):
        # Add identification-str to prevent interference between different tasks of the same class
        # Expand path to account for use of env variables (like $USER)
        return law.wlcg.WLCGDirectoryTarget(
            self.remote_path(f"htcondor_files/{self.nick}_{self.friend_name}"),
            law.wlcg.WLCGFileSystem(
                None, base="{}".format(os.path.expandvars(self.wlcg_path))
            ),
        )

    def htcondor_create_job_file_factory(self):
        task_name = self.__class__.__name__
        task_name = "_".join([task_name, self.nick, self.friend_name])
        _cfg = Config.instance()
        job_file_dir = _cfg.get_expanded("job", "job_file_dir")
        job_files = os.path.join(
            job_file_dir,
            self.production_tag,
            task_name,
            "files",
        )
        factory = super(HTCondorWorkflow, self).htcondor_create_job_file_factory(
            dir=job_files,
            mkdtemp=False,
        )
        return factory

    def htcondor_job_config(self, config, job_num, branches):
        config = super().htcondor_job_config(config, job_num, branches)
        config.custom_content.append(
            (
                "JobBatchName",
                f"{self.nick}-{self.analysis}-{self.friend_name}-{self.production_tag}",
            )
        )
        # update the log file paths
        for type in ["Log", "Output", "Error"]:
            logfilepath = ""
            for param in config.custom_content:
                if param[0] == type:
                    logfilepath = param[1]
                    break
            # split the filename, and add the sample nick as an additional folder
            logfolder = logfilepath.split("/")[:-1]
            logfile = logfilepath.split("/")[-1]
            logfile.replace("_", f"_{self.friend_name}_")
            logfolder.append(self.nick)
            # create the new path folder if it does not exist
            os.makedirs("/".join(logfolder), exist_ok=True)
            config.custom_content.append((type, "/".join(logfolder) + "/" + logfile))
        return config

    def modify_polling_status_line(self, status_line):
        """
        Hook to modify the status line that is printed during polling.
        """
        name = f"{self.nick} (Analysis: {self.analysis} FriendName: {self.friend_name} Tag: {self.production_tag})"
        return f"{status_line} - {law.util.colored(name, color='light_blue')}"

    def workflow_requires(self):
        requirements = {}
        requirements["ntuples"] = CROWNRun(
            nick=self.nick,
            analysis=self.analysis,
            config=self.config,
            production_tag=self.production_tag,
            all_eras=self.all_eras,
            shifts=self.shifts,
            all_sampletypes=self.all_sampletypes,
            era=self.era,
            sampletype=self.sampletype,
            scopes=self.scopes,
        )
        requirements["friend_tarball"] = CROWNBuildFriend.req(self)
        return requirements

    def requires(self):
        return {"friend_tarball": CROWNBuildFriend.req(self)}

    def create_branch_map(self):
        """
        The function `create_branch_map` creates a dictionary `branch_map` that maps file counters to
        various attributes based on the input files.
        :return: a dictionary called `branch_map`.
        """
        branch_map = {}
        counter = 0
        inputs = self.input()["ntuples"]["collection"]
        branches = inputs._flat_target_list
        # get all files from the dataset, including missing ones
        for inputfile in branches:
            if not inputfile.path.endswith(".root"):
                continue
            # identify the scope from the inputfile
            scope = inputfile.path.split("/")[-2]
            if scope in self.scopes:
                branch_map[counter] = {
                    "scope": scope,
                    "nick": self.nick,
                    "era": self.era,
                    "sampletype": self.sampletype,
                    "inputfile": os.path.expandvars(self.wlcg_path) + inputfile.path,
                    "filecounter": int(counter / len(self.scopes)),
                }
                counter += 1
        return branch_map

    def output(self):
        """
        The function `output` generates a file path based on various input parameters and returns the
        corresponding file target.
        :return: The `target` variable is being returned.
        """
        nicks = [
            "{friendname}/{era}/{nick}/{scope}/{nick}_{branch}.root".format(
                friendname=self.friend_name,
                era=self.branch_data["era"],
                nick=self.branch_data["nick"],
                branch=self.branch_data["filecounter"],
                scope=self.branch_data["scope"],
            )
        ]
        # quantities_map json for each scope only needs to be created once per sample
        if self.branch_data["filecounter"] == 0:
            nicks.append(
                "{friendname}/{era}/{nick}/{scope}/{era}_{nick}_{scope}_quantities_map.json".format(
                    friendname=self.friend_name,
                    era=self.branch_data["era"],
                    nick=self.branch_data["nick"],
                    scope=self.branch_data["scope"],
                )
            )

        targets = self.remote_targets(nicks)
        for target in targets:
            target.parent.touch()
        return targets

    def run(self):
        """
        The function runs a CROWN friend executable with specified input and output files, unpacking a
        tarball if necessary, and logs the output and any errors.
        """
        outputs = self.output()
        output = outputs[0]
        branch_data = self.branch_data
        scope = branch_data["scope"]
        era = branch_data["era"]
        sampletype = branch_data["sampletype"]
        quantities_map_outputs = [
            x for x in outputs if x.path.endswith("quantities_map.json")
        ]
        _base_workdir = os.path.abspath("workdir")
        create_abspath(_base_workdir)
        _workdir = os.path.join(
            _base_workdir, f"{self.production_tag}_{self.friend_name}"
        )
        create_abspath(_workdir)
        _inputfile = branch_data["inputfile"]
        # set the outputfilename to the first name in the output list, removing the scope suffix
        _outputfile = str(output.basename.replace("_{}.root".format(scope), ".root"))
        _abs_executable = "{}/{}_{}_{}".format(
            _workdir, self.friend_config, sampletype, era
        )
        console.log(
            "Getting CROWN friend_tarball from {}".format(
                self.input()["friend_tarball"].uri()
            )
        )
        with self.input()["friend_tarball"].localize("r") as _file:
            _tarballpath = _file.path
        # first unpack the tarball if the exec is not there yet
        tempfile = os.path.join(
            _workdir,
            "unpacking_{}_{}_{}".format(self.friend_config, sampletype, era),
        )
        while os.path.exists(tempfile):
            time.sleep(1)
        if not os.path.exists(_abs_executable):
            # create a temp file to signal that we are unpacking
            open(
                tempfile,
                "a",
            ).close()
            tar = tarfile.open(_tarballpath, "r:gz")
            tar.extractall(_workdir)
            os.remove(tempfile)
        # set environment using env script
        my_env = self.set_environment("{}/init.sh".format(_workdir))
        _crown_args = [_outputfile] + [_inputfile]
        _executable = "./{}_{}_{}_{}".format(self.friend_config, sampletype, era, scope)
        # actual payload:
        console.rule("Starting CROWNFriends")
        console.log("Executable: {}".format(_executable))
        console.log("inputfile(s) {}".format(_inputfile))
        console.log("outputfile {}".format(_outputfile))
        console.log("workdir {}".format(_workdir))  # run CROWN
        with subprocess.Popen(
            [_executable] + _crown_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
            env=my_env,
            cwd=_workdir,
        ) as p:
            for line in p.stdout:
                if line != "\n":
                    console.log(line.replace("\n", ""))
            for line in p.stderr:
                if line != "\n":
                    console.log("Error: {}".format(line.replace("\n", "")))
        if p.returncode != 0:
            console.log(
                "Error when running crown {}".format(
                    [_executable] + _crown_args,
                )
            )
            console.log("crown returned non-zero exit status {}".format(p.returncode))
            raise Exception("crown failed")
        else:
            console.log("Successful")
        console.log("Output files afterwards: {}".format(os.listdir(_workdir)))
        output.parent.touch()
        local_filename = os.path.join(
            _workdir,
            _outputfile.replace(".root", "_{}.root".format(scope)),
        )
        # for each outputfile, add the scope suffix
        output.copy_from_local(local_filename)
        if self.branch == 0:
            for i, outputfile in enumerate(quantities_map_outputs):
                outputfile.parent.touch()
                inputfile = os.path.join(
                    _workdir,
                    _outputfile.replace(".root", "_{}.root".format(self.scopes[i])),
                )
                local_outputfile = os.path.join(_workdir, "quantities_map.json")

                self.run_command(
                    command=[
                        "python3",
                        "processor/tasks/helpers/GetQuantitiesMap.py",
                        "--input {}".format(inputfile),
                        "--era {}".format(self.branch_data["era"]),
                        "--scope {}".format(self.scopes[i]),
                        "--sampletype {}".format(self.branch_data["sampletype"]),
                        "--output {}".format(local_outputfile),
                    ],
                    sourcescript=[
                        "{}/init.sh".format(_workdir),
                    ],
                    silent=True,
                )
                # copy the generated quantities_map json to the output
                outputfile.copy_from_local(local_outputfile)
        console.rule("Finished CROWNFriends")
