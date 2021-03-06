from aiida.orm import StructureData
from aiida.orm import Dict
from aiida.orm.nodes.data.array import ArrayData
from aiida.orm import Int, Float, Str, Bool
from aiida.orm import SinglefileData
from aiida.orm import RemoteData
from aiida.orm import Code

from aiida.engine import WorkChain, ToContext, while_

from aiida_cp2k.calculations import Cp2kCalculation

from io import StringIO, BytesIO

from apps.scanning_probe import common

from aiida.plugins import CalculationFactory
StmCalculation = CalculationFactory('spm.stm')

import os
import tempfile
import shutil
import numpy as np

class OrbitalWorkChain(WorkChain):

    @classmethod
    def define(cls, spec):
        super(OrbitalWorkChain, cls).define(spec)
        
        spec.input("cp2k_code", valid_type=Code)
        spec.input("structure", valid_type=StructureData)
        spec.input("wfn_file_path", valid_type=Str, default=Str(""))
        
        spec.input("dft_params", valid_type=Dict)
        
        spec.input("stm_code", valid_type=Code)
        spec.input("stm_params", valid_type=Dict)
        
        spec.outline(
            cls.run_scf_diag,
            cls.run_stm,
            cls.finalize,
        )
        
        spec.outputs.dynamic = True
    
    def run_scf_diag(self):
        self.report("Running CP2K diagonalization SCF")
        
        n_lumo = int(self.inputs.stm_params.get_dict()['--n_lumo'])

        inputs = self.build_cp2k_inputs(self.inputs.structure,
                                        self.inputs.cp2k_code,
                                        self.inputs.dft_params.get_dict(),
                                        self.inputs.wfn_file_path.value,
                                        n_lumo)

        self.report("inputs: "+str(inputs))
        future = self.submit(Cp2kCalculation, **inputs)
        return ToContext(scf_diag=future)
   
           
    def run_stm(self):
        self.report("STM calculation")
             
        inputs = {}
        inputs['metadata'] = {}
        inputs['metadata']['label'] = "orb"
        inputs['code'] = self.inputs.stm_code
        inputs['parameters'] = self.inputs.stm_params
        inputs['parent_calc_folder'] = self.ctx.scf_diag.outputs.remote_folder
        inputs['metadata']['options'] = {
            "resources": {"num_machines": 1},
            "max_wallclock_seconds": 3600,
        } 
        
        # Need to make an explicit instance for the node to be stored to aiida
        settings = Dict(dict={'additional_retrieve_list': ['orb.npz']})
        inputs['settings'] = settings
        
        self.report("Inputs: " + str(inputs))
        
        future = self.submit(StmCalculation, **inputs)
        return ToContext(stm=future)
    
    def finalize(self):
        self.report("Work chain is finished")
    
    
     # ==========================================================================
    @classmethod
    def build_cp2k_inputs(cls, structure, code, dft_params, wfn_file_path, n_lumo):

        inputs = {}
        inputs['code'] = code
        inputs['file'] = {}
        inputs['metadata'] = {}
        
        inputs['metadata']['label'] = "scf_diag"
       
        
        atoms = structure.get_ase()  # slow
        n_atoms = len(atoms)
        
        spin_guess = None
        if dft_params['uks']:
            spin_guess = [dft_params['spin_up_guess'], dft_params['spin_dw_guess']]

        geom_f = cls.make_geom_file(
            atoms, "geom.xyz", spin_guess
        )

        inputs['file']['geom_coords'] = geom_f
        
        bbox = common.get_bbox(atoms)
        extra_space = 15.0 # angstrom
        
        # parameters
        cell_abc = "%f  %f  %f" % (2 * bbox[0] + extra_space,
                                   2 * bbox[1] + extra_space,
                                   2 * bbox[2] + extra_space)
        num_machines = 3
        if n_atoms > 50:
            num_machines = 6
        if n_atoms > 100:
            num_machines = 12
        walltime = 72000
        
        wfn_file = ""
        if wfn_file_path != "":
            wfn_file = os.path.basename(wfn_file_path)
            
        added_mos = max(n_lumo, 50)

        inp = cls.get_cp2k_input(dft_params,
                                 cell_abc,
                                 walltime*0.97,
                                 wfn_file,
                                 added_mos,
                                 atoms)

        inputs['parameters'] = Dict(dict=inp)

        # settings
        settings = Dict(dict={'additional_retrieve_list': [
            'aiida.inp', 'BASIS_MOLOPT', 'geom.xyz', 'aiida-RESTART.wfn'
        ]})
        inputs['settings'] = settings

        # resources
        inputs['metadata']['options'] = {
            "resources": {"num_machines": num_machines},
            "max_wallclock_seconds": walltime,
            "append_text": "cp $CP2K_DATA_DIR/BASIS_MOLOPT .",
            "parser_name": 'cp2k_advanced_parser',
        }
        if wfn_file_path != "":
            inputs['metadata']['options']["prepend_text"] = "cp %s ." % wfn_file_path
        
        return inputs
    
    # ==========================================================================
    @classmethod
    def make_geom_file(cls, atoms, filename, spin_guess=None):
        # spin_guess = [[spin_up_indexes], [spin_down_indexes]]
        tmpdir = tempfile.mkdtemp()
        file_path = tmpdir + "/" + filename

        orig_file = StringIO()
        atoms.write(orig_file, format='xyz')
        orig_file.seek(0)
        all_lines = orig_file.readlines()
        comment = all_lines[1] # with newline character!
        orig_lines = all_lines[2:]
        
        modif_lines = []
        for i_line, line in enumerate(orig_lines):
            new_line = line
            lsp = line.split()
            if spin_guess is not None:
                if i_line in spin_guess[0]:
                    new_line = lsp[0]+"1 " + " ".join(lsp[1:])+"\n"
                if i_line in spin_guess[1]:
                    new_line = lsp[0]+"2 " + " ".join(lsp[1:])+"\n"
            modif_lines.append(new_line)
        
        final_str = "%d\n%s" % (len(atoms), comment) + "".join(modif_lines)

        with open(file_path, 'w') as f:
            f.write(final_str)
        aiida_f = SinglefileData(file=file_path)
        shutil.rmtree(tmpdir)
        return aiida_f

    # ==========================================================================
    @classmethod
    def get_cp2k_input(cls, dft_params, cell_abc, walltime, wfn_file, added_mos, atoms):

        inp = {
            'GLOBAL': {
                'RUN_TYPE': 'ENERGY',
                'WALLTIME': '%d' % walltime,
                'PRINT_LEVEL': 'MEDIUM',
                'EXTENDED_FFT_LENGTHS': ''
            },
            'FORCE_EVAL': cls.get_force_eval_qs_dft(dft_params, cell_abc, wfn_file, added_mos, atoms),
        }
        
        if dft_params['elpa_switch']:
            inp['GLOBAL']['PREFERRED_DIAG_LIBRARY'] = 'ELPA'
            inp['GLOBAL']['ELPA_KERNEL'] = 'AUTO'
            inp['GLOBAL']['DBCSR'] = {'USE_MPI_ALLOCATOR': '.FALSE.'}

        return inp

    # ==========================================================================
    @classmethod
    def get_force_eval_qs_dft(cls, dft_params, cell_abc, wfn_file, added_mos, atoms):
        force_eval = {
            'METHOD': 'Quickstep',
            'DFT': {
                'BASIS_SET_FILE_NAME': 'BASIS_MOLOPT',
                'POTENTIAL_FILE_NAME': 'POTENTIAL',
                'CHARGE': "%d" % dft_params['charge'],
                'QS': {
                    'METHOD': 'GPW',
                    'EXTRAPOLATION': 'ASPC',
                    'EXTRAPOLATION_ORDER': '3',
                    'EPS_DEFAULT': '1.0E-14',
                },
                'MGRID': {
                    'CUTOFF': '%d' % (dft_params['mgrid_cutoff']),
                    'NGRIDS': '5',
                },
                'POISSON': {
                    'PERIODIC': 'NONE',
                    'PSOLVER': 'MT',
                },
                'SCF': {
                    'MAX_SCF': '1000',
                    'SCF_GUESS': 'RESTART',
                    'EPS_SCF': '1.0E-6',
                    'ADDED_MOS': str(added_mos),
                    'CHOLESKY': 'INVERSE',
                    'DIAGONALIZATION': {
                        '_': '',
                    },
#                    'SMEAR': {
#                        'METHOD': 'FERMI_DIRAC',
#                        'ELECTRONIC_TEMPERATURE': '300',
#                    },
                    'MIXING': {
                        'METHOD': 'BROYDEN_MIXING',
                        'ALPHA': '0.2',
                        'BETA': '1.5',
                        'NBROYDEN': '8',
                    },
                    'OUTER_SCF': {
                        'MAX_SCF': '15',
                        'EPS_SCF': '1.0E-6',
                    },
                    'PRINT': {
                        'RESTART': {
                            'EACH': {
                                'QS_SCF': '0',
                                'GEO_OPT': '1',
                            },
                            'ADD_LAST': 'NUMERIC',
                            'FILENAME': 'RESTART'
                        },
                        'RESTART_HISTORY': {'_': 'OFF'}
                    }
                },
                'XC': {
                    'XC_FUNCTIONAL': {'_': 'PBE'},
                },
                'PRINT': {
                    'V_HARTREE_CUBE': {
                        'FILENAME': 'HART',
                        'STRIDE': '2 2 2',
                    },
                    'MO_CUBES': {
                        'NHOMO': '5',
                        'NLUMO': '1',
                    },
                    'E_DENSITY_CUBE': {
                        'FILENAME': 'RHO',
                    },
                },
            },
            'SUBSYS': {
                'CELL': {'ABC': cell_abc, 'PERIODIC': 'NONE'},
                'TOPOLOGY': {
                    'COORD_FILE_NAME': 'geom.xyz',
                    'COORDINATE': 'xyz',
                    'CENTER_COORDINATES': {'_': ''},
                },
                'KIND': [],
            }
        }
        
        if wfn_file != "":
            force_eval['DFT']['RESTART_FILE_NAME'] = "./%s"%wfn_file
            #force_eval['DFT']['SCF']['SCF_GUESS'] = 'RESTART'
        
        used_kinds = np.unique(atoms.get_chemical_symbols())
        for symbol in used_kinds:
            force_eval['SUBSYS']['KIND'].append({
                '_': symbol,
                'BASIS_SET': common.ATOMIC_KIND_INFO[symbol]['basis'],
                'POTENTIAL': common.ATOMIC_KIND_INFO[symbol]['pseudo'],
            })
        
        if dft_params['smearing']:
            force_eval['DFT']['SCF']['SMEAR'] = {
                'METHOD': 'FERMI_DIRAC',
                'ELECTRONIC_TEMPERATURE': str(dft_params['smear_t']),
            }
        
        if dft_params['uks']:
            force_eval['DFT']['UKS'] = ''
            force_eval['DFT']['MULTIPLICITY'] = dft_params['multiplicity']
            
            spin_up_indexes = dft_params['spin_up_guess']
            spin_dw_indexes = dft_params['spin_dw_guess']
            
            for i_s, spin_indexes in enumerate([spin_up_indexes, spin_dw_indexes]):
                spin_digit = i_s + 1
                a_nel =  1 if i_s == 0 else -1
                b_nel = -1 if i_s == 0 else  1
                used_kinds = np.unique([atoms.get_chemical_symbols()[i_s] for i_s in spin_indexes])
                for symbol in used_kinds:
                    force_eval['SUBSYS']['KIND'].append({
                        '_': symbol+str(spin_digit),
                        'ELEMENT': symbol,
                        'BASIS_SET': common.ATOMIC_KIND_INFO[symbol]['basis'],
                        'POTENTIAL': common.ATOMIC_KIND_INFO[symbol]['pseudo'],
                        'BS': {
                            'ALPHA': {'NEL': a_nel, 'L': 1, 'N': 2},
                            'BETA':  {'NEL': b_nel, 'L': 1, 'N': 2},
                        },
                    })
                

        return force_eval
    
    
    
