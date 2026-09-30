# -*- coding: utf-8 -*-
"""SOMA 骨架表（关节名 + 父节点）—— **自动生成，不要手改**。

生成器：`scripts/gen_soma_tables.py`
真值来源：D:/kimodo-motion/src/kimodo/kimodo/skeleton/definitions.py

Kimodo 升级后重跑生成器即可。手抄 77 条关节名是必错的，所以走生成。
"""

# (关节名, 父节点名（根为 None）)

# SOMASkeleton77（somaskel77）—— 77 个关节
SOMASkeleton77_JOINTS = [
    ('Hips', None),
    ('Spine1', 'Hips'),
    ('Spine2', 'Spine1'),
    ('Chest', 'Spine2'),
    ('Neck1', 'Chest'),
    ('Neck2', 'Neck1'),
    ('Head', 'Neck2'),
    ('HeadEnd', 'Head'),
    ('Jaw', 'Head'),
    ('LeftEye', 'Head'),
    ('RightEye', 'Head'),
    ('LeftShoulder', 'Chest'),
    ('LeftArm', 'LeftShoulder'),
    ('LeftForeArm', 'LeftArm'),
    ('LeftHand', 'LeftForeArm'),
    ('LeftHandThumb1', 'LeftHand'),
    ('LeftHandThumb2', 'LeftHandThumb1'),
    ('LeftHandThumb3', 'LeftHandThumb2'),
    ('LeftHandThumbEnd', 'LeftHandThumb3'),
    ('LeftHandIndex1', 'LeftHand'),
    ('LeftHandIndex2', 'LeftHandIndex1'),
    ('LeftHandIndex3', 'LeftHandIndex2'),
    ('LeftHandIndex4', 'LeftHandIndex3'),
    ('LeftHandIndexEnd', 'LeftHandIndex4'),
    ('LeftHandMiddle1', 'LeftHand'),
    ('LeftHandMiddle2', 'LeftHandMiddle1'),
    ('LeftHandMiddle3', 'LeftHandMiddle2'),
    ('LeftHandMiddle4', 'LeftHandMiddle3'),
    ('LeftHandMiddleEnd', 'LeftHandMiddle4'),
    ('LeftHandRing1', 'LeftHand'),
    ('LeftHandRing2', 'LeftHandRing1'),
    ('LeftHandRing3', 'LeftHandRing2'),
    ('LeftHandRing4', 'LeftHandRing3'),
    ('LeftHandRingEnd', 'LeftHandRing4'),
    ('LeftHandPinky1', 'LeftHand'),
    ('LeftHandPinky2', 'LeftHandPinky1'),
    ('LeftHandPinky3', 'LeftHandPinky2'),
    ('LeftHandPinky4', 'LeftHandPinky3'),
    ('LeftHandPinkyEnd', 'LeftHandPinky4'),
    ('RightShoulder', 'Chest'),
    ('RightArm', 'RightShoulder'),
    ('RightForeArm', 'RightArm'),
    ('RightHand', 'RightForeArm'),
    ('RightHandThumb1', 'RightHand'),
    ('RightHandThumb2', 'RightHandThumb1'),
    ('RightHandThumb3', 'RightHandThumb2'),
    ('RightHandThumbEnd', 'RightHandThumb3'),
    ('RightHandIndex1', 'RightHand'),
    ('RightHandIndex2', 'RightHandIndex1'),
    ('RightHandIndex3', 'RightHandIndex2'),
    ('RightHandIndex4', 'RightHandIndex3'),
    ('RightHandIndexEnd', 'RightHandIndex4'),
    ('RightHandMiddle1', 'RightHand'),
    ('RightHandMiddle2', 'RightHandMiddle1'),
    ('RightHandMiddle3', 'RightHandMiddle2'),
    ('RightHandMiddle4', 'RightHandMiddle3'),
    ('RightHandMiddleEnd', 'RightHandMiddle4'),
    ('RightHandRing1', 'RightHand'),
    ('RightHandRing2', 'RightHandRing1'),
    ('RightHandRing3', 'RightHandRing2'),
    ('RightHandRing4', 'RightHandRing3'),
    ('RightHandRingEnd', 'RightHandRing4'),
    ('RightHandPinky1', 'RightHand'),
    ('RightHandPinky2', 'RightHandPinky1'),
    ('RightHandPinky3', 'RightHandPinky2'),
    ('RightHandPinky4', 'RightHandPinky3'),
    ('RightHandPinkyEnd', 'RightHandPinky4'),
    ('LeftLeg', 'Hips'),
    ('LeftShin', 'LeftLeg'),
    ('LeftFoot', 'LeftShin'),
    ('LeftToeBase', 'LeftFoot'),
    ('LeftToeEnd', 'LeftToeBase'),
    ('RightLeg', 'Hips'),
    ('RightShin', 'RightLeg'),
    ('RightFoot', 'RightShin'),
    ('RightToeBase', 'RightFoot'),
    ('RightToeEnd', 'RightToeBase'),
]

# SOMASkeleton30（somaskel30）—— 30 个关节
SOMASkeleton30_JOINTS = [
    ('Hips', None),
    ('Spine1', 'Hips'),
    ('Spine2', 'Spine1'),
    ('Chest', 'Spine2'),
    ('Neck1', 'Chest'),
    ('Neck2', 'Neck1'),
    ('Head', 'Neck2'),
    ('Jaw', 'Head'),
    ('LeftEye', 'Head'),
    ('RightEye', 'Head'),
    ('LeftShoulder', 'Chest'),
    ('LeftArm', 'LeftShoulder'),
    ('LeftForeArm', 'LeftArm'),
    ('LeftHand', 'LeftForeArm'),
    ('LeftHandThumbEnd', 'LeftHand'),
    ('LeftHandMiddleEnd', 'LeftHand'),
    ('RightShoulder', 'Chest'),
    ('RightArm', 'RightShoulder'),
    ('RightForeArm', 'RightArm'),
    ('RightHand', 'RightForeArm'),
    ('RightHandThumbEnd', 'RightHand'),
    ('RightHandMiddleEnd', 'RightHand'),
    ('LeftLeg', 'Hips'),
    ('LeftShin', 'LeftLeg'),
    ('LeftFoot', 'LeftShin'),
    ('LeftToeBase', 'LeftFoot'),
    ('RightLeg', 'Hips'),
    ('RightShin', 'RightLeg'),
    ('RightFoot', 'RightShin'),
    ('RightToeBase', 'RightFoot'),
]


SKELETONS = {
    'somaskel77': {
        'class': 'SOMASkeleton77',
        'joints': SOMASkeleton77_JOINTS,
        'chains': {
            'right_foot_joint_names': ['RightFoot', 'RightToeBase', 'RightToeEnd'],
            'left_foot_joint_names': ['LeftFoot', 'LeftToeBase', 'LeftToeEnd'],
            'right_hand_joint_names': ['RightHand', 'RightHandThumb1', 'RightHandThumb2', 'RightHandThumb3', 'RightHandThumbEnd', 'RightHandIndex1', 'RightHandIndex2', 'RightHandIndex3', 'RightHandIndex4', 'RightHandIndexEnd', 'RightHandMiddle1', 'RightHandMiddle2', 'RightHandMiddle3', 'RightHandMiddle4', 'RightHandMiddleEnd', 'RightHandRing1', 'RightHandRing2', 'RightHandRing3', 'RightHandRing4', 'RightHandRingEnd', 'RightHandPinky1', 'RightHandPinky2', 'RightHandPinky3', 'RightHandPinky4', 'RightHandPinkyEnd'],
            'left_hand_joint_names': ['LeftHand', 'LeftHandThumb1', 'LeftHandThumb2', 'LeftHandThumb3', 'LeftHandThumbEnd', 'LeftHandIndex1', 'LeftHandIndex2', 'LeftHandIndex3', 'LeftHandIndex4', 'LeftHandIndexEnd', 'LeftHandMiddle1', 'LeftHandMiddle2', 'LeftHandMiddle3', 'LeftHandMiddle4', 'LeftHandMiddleEnd', 'LeftHandRing1', 'LeftHandRing2', 'LeftHandRing3', 'LeftHandRing4', 'LeftHandRingEnd', 'LeftHandPinky1', 'LeftHandPinky2', 'LeftHandPinky3', 'LeftHandPinky4', 'LeftHandPinkyEnd'],
        },
    },
    'somaskel30': {
        'class': 'SOMASkeleton30',
        'joints': SOMASkeleton30_JOINTS,
        'chains': {
            'right_foot_joint_names': ['RightFoot', 'RightToeBase'],
            'left_foot_joint_names': ['LeftFoot', 'LeftToeBase'],
            'right_hand_joint_names': ['RightHand', 'RightHandMiddleEnd'],
            'left_hand_joint_names': ['LeftHand', 'LeftHandMiddleEnd'],
        },
    },
}


def joint_count(skel_name):
    """按 NPZ 的关节数反推骨架名（30 或 77）。"""
    for name, rec in SKELETONS.items():
        if len(rec['joints']) == skel_name:
            return name
    return None


def joints_for_count(n):
    """给关节数（30 / 77），返回 (骨架名, [(关节名, 父节点名), ...])。"""
    for name, rec in SKELETONS.items():
        if len(rec['joints']) == n:
            return name, list(rec['joints'])
    return None, None


def root_joint(skel_name):
    """根关节名（唯一 parent 为 None 的那个）。"""
    for n, p in SKELETONS[skel_name]['joints']:
        if p is None:
            return n
    return None

